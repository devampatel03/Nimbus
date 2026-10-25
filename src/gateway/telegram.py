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
  and goes through the rate limiter.
- Transcription is NOT done here: voice bytes are handed to the ingest layer,
  which calls the `llm` module.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from src.gateway.api import TelegramApi
from src.gateway.parse import parse_update
from src.gateway.ratelimit import RateLimiter
from src.gateway.render import MAX_MESSAGE_CHARS, escape_html, render_chunks, sanitize
from src.gateway.settings import GatewaySettings
from src.gateway.store import Seen, UpdateStore
from src.gateway.types import (
    ApprovalCard,
    FileRef,
    FileTooLarge,
    InboundUpdate,
    MessageRef,
    MessageTooLong,
    TelegramAuthError,
    TelegramError,
    UpdateKind,
)
from src.net import EgressClient, EgressError

logger = logging.getLogger("nimbus.gateway")

_MAX_BACKOFF_S = 30.0
_CALLBACK_DATA_MAX_BYTES = 64


class TelegramGateway:
    def __init__(
        self,
        settings: GatewaySettings,
        egress: EgressClient,
        store: UpdateStore,
        *,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._s = settings
        self._chat = settings.allowed_user_id  # private chat id == user id
        self._store = store
        self._sleep = sleep
        self._limiter = limiter or RateLimiter(
            per_chat_interval_s=settings.per_chat_interval_s,
            global_interval_s=settings.global_interval_s,
            sleep=sleep,
        )
        self._api = TelegramApi(egress, settings, sleep=sleep)
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True

    # ------------------------------------------------------------ inbound

    async def inbound(self) -> AsyncIterator[InboundUpdate]:
        backoff = 1.0
        while not self._closed:
            try:
                offset = await self._store.get_offset()
                raw_updates = await self._api.get_updates(offset)
                backoff = 1.0
            except TelegramAuthError:
                logger.error("telegram auth failed; stopping poll loop")
                raise
            except (TelegramError, EgressError) as exc:
                logger.warning("poll failed reason=%s backoff=%.0fs", _reason(exc), backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue
            await asyncio.sleep(0)  # always cede the loop (empty polls must not spin)
            for raw in raw_updates:
                if self._closed:
                    return
                item = await self._admit(raw)
                if item is not None:
                    yield item

    async def _admit(self, raw: Any) -> InboundUpdate | None:
        parsed = parse_update(raw, self._s.allowed_user_id)
        if parsed is None:
            logger.warning("discarded update without usable update_id")
            return None
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
            return None
        seen = await self._store.begin(parsed.update_id, parsed.from_user_id, parsed.kind.value)
        if seen is Seen.DONE:
            # Telegram resent something we already finished (offset lagged).
            await self._store.complete(parsed.update_id)
            return None
        return parsed.update

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

    async def send_text(self, text: str, *, reply_to: MessageRef | None = None) -> list[MessageRef]:
        refs: list[MessageRef] = []
        for i, html in enumerate(render_chunks(text)):
            refs.append(await self._send_one(html, reply_to=reply_to if i == 0 else None))
        return refs

    async def send_approval(self, card: ApprovalCard) -> MessageRef:
        for data in (card.approve_data, card.reject_data):
            n = len(data.encode("utf-8"))
            if not 1 <= n <= _CALLBACK_DATA_MAX_BYTES:
                raise ValueError("callback_data must be 1..64 bytes")
        body = (
            "Approval needed. Exact payload:\n<pre>" + escape_html(sanitize(card.text)) + "</pre>"
        )
        if len(body) > MAX_MESSAGE_CHARS:
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

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None:
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
