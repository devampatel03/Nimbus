# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Telegram `Gateway` over long polling (R1, R2, R3).

- Outbound-only: `getUpdates` long polls; nothing listens on a port (R3).
- R2: only `TELEGRAM_ALLOWED_USER_ID` is served. Anyone else's update (message
  or callback) is dropped with no reply, no `answerCallbackQuery`, and one
  audit log line holding update id, sender id and kind only -- never content.
- R1: see `store.py`; the offset advances only with the ack of the event the
  update produced. Updates are processed strictly in order.
- Every send disables link previews, uses HTML parse mode with full escaping,
  and goes through the rate limiter. `_send_one` is the only send call site.
- P4.3: every `send_text` (model replies, reminders, notices) and keypad
  prompt passes the exfil filter (`exfil.py`); each chunk is re-checked and a
  failing reply is replaced by a fixed notice. Approval cards are NOT filtered:
  they must show the exact payload being approved; it is rendered inside one
  `<pre>` entity, previews off, and a payload holding a non-allowlisted link
  gets a fixed warning line outside the `<pre>` (host never echoed). That
  Telegram clients do not make a URL inside `<pre>` tappable is a live check
  (HUMAN_TODO). Callback toasts (`answer_callback`) carry code constants only
  (static test).
- Transcription is NOT done here: voice bytes are handed to the ingest layer,
  which calls the `llm` module.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import timedelta, timezone
from typing import Any

from src.gateway.api import TelegramApi
from src.gateway.exfil import BLOCKED_NOTICE, filter_reply, find_links
from src.gateway.parse import parse_update
from src.gateway.ratelimit import RateLimiter
from src.gateway.render import (
    MAX_MESSAGE_CHARS,
    chunk_text,
    escape_html,
    sanitize,
    utf16_len,
)
from src.gateway.settings import GatewaySettings
from src.gateway.store import Seen, UpdateStore
from src.gateway.types import (
    ApprovalCard,
    FileRef,
    FileTooLarge,
    InboundUpdate,
    MessageRef,
    MessageTooLong,
    OutboundBlocked,
    TelegramAuthError,
    TelegramError,
    UpdateKind,
)
from src.net import EgressClient, EgressError

logger = logging.getLogger("nimbus.gateway")

_MAX_BACKOFF_S = 30.0
_CALLBACK_DATA_MAX_BYTES = 64
# Fixed text: no message content, only the time the user sent it (M1).
_POISON_NOTICE = "I couldn't process a message you sent at {hhmm}, please resend"
# Fixed line shown above an approval card whose payload holds a link or web
# address not on the reply allowlist. Never names the host (M2).
APPROVAL_LINK_WARNING = (
    "Warning: this payload contains a web address. Check it before approving; do not open it."
)


class TelegramGateway:
    def __init__(
        self,
        settings: GatewaySettings,
        egress: EgressClient,
        store: UpdateStore,
        *,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_poll: Callable[[], None] | None = None,
    ) -> None:
        self._s = settings
        self._reply_policy = settings.reply_url_policy()
        # Liveness signal for the app heartbeat: called after every COMPLETED
        # getUpdates cycle (empty long polls included), never on a failed one.
        self._on_poll = on_poll
        self._chat = settings.allowed_user_id  # private chat id == user id
        self._store = store
        self._sleep = sleep
        self._limiter = limiter or RateLimiter(
            per_chat_interval_s=settings.per_chat_interval_s,
            global_interval_s=settings.global_interval_s,
            sleep=sleep,
        )
        self._api = TelegramApi(
            egress,
            settings,
            sleep=sleep,
            on_retry_after=lambda secs: self._limiter.penalize(self._chat, secs),
        )
        self._closed = False
        self._notices_due = True  # retry unsent poison notices after a restart

    async def aclose(self) -> None:
        self._closed = True

    # ------------------------------------------------------------ inbound

    async def inbound(self) -> AsyncIterator[InboundUpdate]:
        backoff = 1.0
        stall = 1.0
        while not self._closed:
            if self._notices_due:
                await self._flush_poison_notices()
            try:
                offset = await self._store.get_offset()
                raw_updates = await self._api.get_updates(offset)
                backoff = 1.0
                if self._on_poll is not None:
                    self._on_poll()
            except TelegramAuthError:
                logger.error("telegram auth failed; stopping poll loop")
                raise
            except (TelegramError, EgressError) as exc:
                logger.warning("poll failed reason=%s backoff=%.0fs", _reason(exc), backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue
            await asyncio.sleep(0)  # always cede the loop (empty polls must not spin)
            progressed = not raw_updates
            for raw in raw_updates:
                if self._closed:
                    return
                item, fresh = await self._admit(raw)
                progressed = progressed or fresh
                if item is not None:
                    yield item
            if self._closed:
                return
            if not progressed:
                # Everything returned was already delivered and not yet acked
                # (or already done): do not re-poll in a hot loop.
                await self._sleep(stall)
                stall = min(stall * 2, _MAX_BACKOFF_S)
            else:
                stall = 1.0

    async def _admit(self, raw: Any) -> tuple[InboundUpdate | None, bool]:
        """Returns (update to deliver, made_progress). Progress means new
        information: a first delivery, a drop, or a poison resolution."""
        parsed = parse_update(raw, self._s.allowed_user_id)
        if parsed is None:
            logger.warning("discarded update without usable update_id")
            return None, True
        if parsed.update is None:
            level = logging.WARNING if parsed.kind is UpdateKind.DROPPED else logging.INFO
            logger.log(
                level,
                "update not served update_id=%d from_id=%s kind=%s",
                parsed.update_id,
                parsed.from_user_id,
                parsed.kind.value,
            )
            await self._store.record_dropped(
                parsed.update_id, parsed.from_user_id, parsed.kind.value
            )
            return None, True
        seen = await self._store.begin(parsed.update_id, parsed.from_user_id, parsed.kind.value)
        if seen is Seen.DONE:
            # Telegram resent something we already finished (offset lagged).
            await self._store.complete(parsed.update_id)
            return None, False
        if seen is Seen.POISON:
            logger.error(
                "poison update dropped after redelivery budget update_id=%d", parsed.update_id
            )
            self._notices_due = True
            await self._flush_poison_notices()
            return None, True
        return parsed.update, seen is Seen.NEW

    async def _flush_poison_notices(self) -> None:
        """Tell the user, once per poisoned update, that a message was dropped
        (M1). The pending record is cleared only after the send succeeded, so a
        failed send is retried on the next poll cycle and survives a restart."""
        self._notices_due = False
        try:
            pending = await self._store.pending_poison_notices()
        except Exception as exc:  # noqa: BLE001 - never break polling over a notice
            logger.warning("poison notice lookup failed err=%s", type(exc).__name__)
            self._notices_due = True
            return
        tz = timezone(timedelta(minutes=self._s.notice_utc_offset_minutes))
        for update_id, received_at in pending:
            hhmm = received_at.astimezone(tz).strftime("%H:%M")
            try:
                await self.send_text(_POISON_NOTICE.format(hhmm=hhmm))
                await self._store.clear_poison_notice(update_id)
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                logger.warning("poison notice failed err=%s", type(exc).__name__)
                self._notices_due = True
                return

    async def ack(self, update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None:
        await self._store.complete(update.update_id, event_id=event_id)

    # ----------------------------------------------------------- outbound

    async def _send_one(
        self, html: str, *, reply_to: MessageRef | None, markup: dict[str, Any] | None = None
    ) -> MessageRef:
        await self._limiter.acquire(self._chat)
        payload: dict[str, Any] = {
            "chat_id": self._chat,
            "text": html,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to.message_id,
                "allow_sending_without_reply": True,
            }
        if markup is not None:
            payload["reply_markup"] = markup
        result = await self._api.call("sendMessage", payload)
        mid = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(mid, int) or isinstance(mid, bool):
            raise TelegramError("bad_response")
        return MessageRef(mid)

    def _outbound_chunks(self, text: str) -> list[str]:
        """Every outbound text check, in one place (P4.3 link filter; later
        outbound checks such as P4.4 canaries belong here). Returns raw chunks;
        a failing reply becomes the fixed notice. Logs counts only: the reply
        may hold the very data being exfiltrated."""
        result = filter_reply(text, self._reply_policy)
        chunks = chunk_text(result.text)
        if result.blocked or any(find_links(c, self._reply_policy) for c in chunks):
            logger.warning("outbound reply blocked by link filter removed=%d", result.removed)
            return chunk_text(BLOCKED_NOTICE)
        if result.removed:
            logger.info("outbound links neutralised count=%d", result.removed)
        return chunks

    async def send_text(self, text: str, *, reply_to: MessageRef | None = None) -> list[MessageRef]:
        refs: list[MessageRef] = []
        for i, chunk in enumerate(self._outbound_chunks(text)):
            refs.append(
                await self._send_one(escape_html(chunk), reply_to=reply_to if i == 0 else None)
            )
        return refs

    async def send_approval(self, card: ApprovalCard) -> MessageRef:
        for data in (card.approve_data, card.reject_data):
            n = len(data.encode("utf-8"))
            if not 1 <= n <= _CALLBACK_DATA_MAX_BYTES:
                raise ValueError("callback_data must be 1..64 bytes")
        # Fixed text outside one <pre>; the payload only inside it. A payload
        # with a non-allowlisted link gets a fixed warning (host never echoed).
        warn = APPROVAL_LINK_WARNING + "\n" if find_links(card.text, self._reply_policy) else ""
        body = (
            warn
            + "Approval needed. Exact payload:\n<pre>"
            + escape_html(sanitize(card.text))
            + "</pre>"
        )
        if utf16_len(body) > MAX_MESSAGE_CHARS:
            # Never truncate what the user is approving.
            raise MessageTooLong("approval_payload_too_long")
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": card.approve_data},
                    {"text": "Reject", "callback_data": card.reject_data},
                ]
            ]
        }
        return await self._send_one(body, reply_to=None, markup=markup)

    async def send_keypad(self, text: str, rows: Sequence[Sequence[tuple[str, str]]]) -> MessageRef:
        """A message with an inline keypad (approval step-up). Labels are fixed
        by the approvals layer; every callback_data is size-checked."""
        keyboard: list[list[dict[str, str]]] = []
        for row in rows:
            buttons: list[dict[str, str]] = []
            for label, data in row:
                if not 1 <= len(data.encode("utf-8")) <= _CALLBACK_DATA_MAX_BYTES:
                    raise ValueError("callback_data must be 1..64 bytes")
                buttons.append({"text": sanitize(label)[:16], "callback_data": data})
            keyboard.append(buttons)
        filtered = filter_reply(text, self._reply_policy)
        if filtered.blocked:
            logger.warning("keypad text blocked by link filter")
            raise OutboundBlocked("outbound_blocked")
        body = escape_html(filtered.text)
        if utf16_len(body) > MAX_MESSAGE_CHARS:
            raise MessageTooLong("keypad_text_too_long")
        return await self._send_one(body, reply_to=None, markup={"inline_keyboard": keyboard})

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None:
        await self._limiter.acquire(self._chat)
        await self._api.call(
            "editMessageReplyMarkup",
            {
                "chat_id": self._chat,
                "message_id": ref.message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )
        await self.send_text(f"Approval {outcome}", reply_to=ref)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self._limiter.acquire(self._chat)
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = sanitize(text)[:200]
        await self._api.call("answerCallbackQuery", payload)

    async def fetch_file(self, file: FileRef, *, max_bytes: int) -> bytes:
        cap = (
            self._s.max_voice_bytes if file.kind is UpdateKind.VOICE else self._s.max_document_bytes
        )
        limit = min(max_bytes, cap)
        if limit <= 0:
            raise FileTooLarge("file_too_large")
        if file.size is not None and file.size > limit:
            raise FileTooLarge("file_too_large")
        return await self._api.download(file.file_id, max_bytes=limit)


def _reason(exc: Exception) -> str:
    return getattr(exc, "reason", type(exc).__name__)
