# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`CanaryGateway`: a `Gateway` decorator that screens every outbound Telegram
text through the `CanaryGuard` (P4.4). Inbound, ack and file fetch pass
through untouched.

* `send_text`: a hit sends `BLOCKED_REPLY` instead (the user still gets an
  answer, the canary never leaves). Checked as one stream with the previous
  sends, so a canary split across messages is caught.
* `send_approval` / `send_keypad`: a hit raises `CanaryBlocked`; the approvals
  layer treats a card it could not send as FAILED and cancels the row.
* `answer_callback`: a hit drops the toast text.
* `close_approval`: the outcome is a fixed code, still checked (hit: raise).

Kept separate from the send-path renderer (P4.3) on purpose: it composes with
whatever gateway it wraps.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence

from src.canary.guard import BLOCKED_REPLY, CanaryGuard
from src.gateway.protocols import Gateway
from src.gateway.types import ApprovalCard, FileRef, InboundUpdate, MessageRef


class CanaryGateway:
    def __init__(self, inner: Gateway, guard: CanaryGuard) -> None:
        self._inner = inner
        self._guard = guard

    @property
    def inner(self) -> Gateway:
        """The unwrapped gateway (the alert hook sends its fixed text here)."""
        return self._inner

    def inbound(self) -> AsyncIterator[InboundUpdate]:
        return self._inner.inbound()

    async def ack(self, update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None:
        await self._inner.ack(update, event_id=event_id)

    async def send_text(self, text: str, *, reply_to: MessageRef | None = None) -> list[MessageRef]:
        if not await self._guard.screen(text, sink="telegram_send", stream=True):
            text = BLOCKED_REPLY
        return await self._inner.send_text(text, reply_to=reply_to)

    async def send_approval(self, card: ApprovalCard) -> MessageRef:
        await self._guard.require_clean(card.text, sink="telegram_card")
        return await self._inner.send_approval(card)

    async def send_keypad(self, text: str, rows: Sequence[Sequence[tuple[str, str]]]) -> MessageRef:
        labels = " ".join(label for row in rows for label, _ in row)
        await self._guard.require_clean(f"{text}\n{labels}", sink="telegram_keypad")
        return await self._inner.send_keypad(text, rows)

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None:
        await self._guard.require_clean(outcome, sink="telegram_close")
        await self._inner.close_approval(ref, outcome=outcome)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        if text is not None and not await self._guard.screen(text, sink="telegram_toast"):
            text = None
        await self._inner.answer_callback(callback_id, text)

    async def fetch_file(self, file: FileRef, *, max_bytes: int) -> bytes:
        return await self._inner.fetch_file(file, max_bytes=max_bytes)
