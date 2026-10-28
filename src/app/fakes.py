# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""In-process stand-ins for the network backends, for `APP_FAKE_BACKENDS=true`
(development only; the entrypoint refuses it in production). They let
`python -m src.app` start, loop, heartbeat and shut down with no Telegram token,
no model provider and no network. Nothing here is imported by the production
path unless that switch is on.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Sequence

from src.gateway.types import (
    ApprovalCard,
    FileRef,
    FileTooLarge,
    InboundUpdate,
    MessageRef,
)
from src.llm.types import ChatRequest, ProviderResponse, Usage


class DevProvider:
    """Model stand-in for `APP_FAKE_BACKENDS`: a canned chat reply, and `{}` for any
    structured-output call (memory extraction), so the dev process never dead-letters
    jobs on a non-JSON answer."""

    name = "dev-fake"
    model = "dev-fake"

    async def complete(self, req: ChatRequest) -> ProviderResponse:
        text = "{}" if req.response_schema is not None else "fake reply"
        return ProviderResponse(text=text, usage=Usage(tokens_in=10, tokens_out=3))


class InMemoryGateway:
    """A `Gateway` over an asyncio queue. `push()` feeds updates in; sent text is
    recorded in `sent`; acks in `acked`."""

    def __init__(
        self, *, on_poll: Callable[[], None] | None = None, poll_interval_s: float = 0.5
    ) -> None:
        self._on_poll = on_poll
        self._poll_s = poll_interval_s
        self._inbox: asyncio.Queue[InboundUpdate] = asyncio.Queue()
        self.sent: list[str] = []
        self.acked: list[tuple[int, uuid.UUID | None]] = []
        self.callbacks: list[tuple[str, str | None]] = []
        self.cards: list[ApprovalCard] = []
        self.keypads: list[list[list[tuple[str, str]]]] = []
        self.closed = False
        self._next = 1000

    def push(self, update: InboundUpdate) -> None:
        self._inbox.put_nowait(update)

    async def inbound(self) -> AsyncIterator[InboundUpdate]:
        while not self.closed:
            try:
                update = await asyncio.wait_for(self._inbox.get(), timeout=self._poll_s)
            except TimeoutError:
                update = None  # an empty "long poll"
            if self._on_poll is not None:
                self._on_poll()
            if update is not None:
                yield update

    async def aclose(self) -> None:
        self.closed = True

    async def ack(self, update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None:
        self.acked.append((update.update_id, event_id))

    def _ref(self) -> MessageRef:
        self._next += 1
        return MessageRef(self._next)

    async def send_text(self, text: str, *, reply_to: MessageRef | None = None) -> list[MessageRef]:
        self.sent.append(text)
        return [self._ref()]

    async def send_approval(self, card: ApprovalCard) -> MessageRef:
        self.sent.append(card.text)
        self.cards.append(card)
        return self._ref()

    async def send_keypad(self, text: str, rows: Sequence[Sequence[tuple[str, str]]]) -> MessageRef:
        self.sent.append(text)
        self.keypads.append([list(r) for r in rows])
        return self._ref()

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None:
        self.sent.append(f"Approval {outcome}")

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.callbacks.append((callback_id, text))

    async def fetch_file(self, file: FileRef, *, max_bytes: int) -> bytes:
        raise FileTooLarge("file_too_large")
