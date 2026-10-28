# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The `Gateway` protocol (TARGET_DESIGN section 3). A PWA channel can later
implement the same shape.

Delivery contract: `inbound()` yields each allowlisted update at least once.
The consumer must `ack()` an update once it is durably handled (its event
stored); only then does the persisted offset move past it. An update that is
not acked is redelivered after a restart or on the next poll."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from src.gateway.types import ApprovalCard, FileRef, InboundUpdate, MessageRef


class Gateway(Protocol):
    def inbound(self) -> AsyncIterator[InboundUpdate]: ...

    async def ack(self, update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None: ...

    async def send_text(
        self, text: str, *, reply_to: MessageRef | None = None
    ) -> list[MessageRef]: ...

    async def send_approval(self, card: ApprovalCard) -> MessageRef: ...

    async def send_keypad(
        self, text: str, rows: Sequence[Sequence[tuple[str, str]]]
    ) -> MessageRef: ...

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None: ...

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...

    async def fetch_file(self, file: FileRef, *, max_bytes: int) -> bytes: ...
