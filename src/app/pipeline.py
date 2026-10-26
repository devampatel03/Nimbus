# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Gateway loop plumbing: ingest first, then the turn.

`Ingestor.run()` owns the order store -> ack -> follow-on. The turn is hooked in
by wrapping the gateway: `TurnGateway.ack()` delegates the ack FIRST and only
then queues the update for the turn worker. So the event is stored and acked
before the model or Telegram is touched for the reply (consumer ordering, R1,
ADR-0016), and the poll/ingest loop never waits on an LLM call.

Reply delivery is at-most-once: a crash after the ack but before the reply
loses the reply (never the event). The user is told once when a turn fails; a
turn orphaned by a crash is a recorded residual risk (see the PR).

Only the user's own text starts a turn. Forwards / via-bot text
(`update.untrusted`) is stored in the untrusted lane and never becomes a trusted
instruction (INV-6); voice and documents are ingest-only until a transcriber
exists.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from src.gateway.protocols import Gateway
from src.gateway.types import (
    ApprovalCard,
    FileRef,
    InboundUpdate,
    MessageRef,
    TelegramAuthError,
    UpdateKind,
)
from src.ingest.sanitize import clean_text
from src.llm.types import ChatMessage
from src.orchestrator.loop import Orchestrator, TurnResult

log = logging.getLogger(__name__)

TURN_FAILED_MESSAGE = "I couldn't answer that just now. Please try again in a moment."
STALE_CALLBACK_MESSAGE = "That action is no longer available."
MAX_TURN_TEXT_CHARS = 16_000


@dataclass(frozen=True, slots=True)
class PendingTurn:
    update: InboundUpdate
    event_id: uuid.UUID | None


class TurnGateway:
    """A `Gateway` that delegates everything and queues turns after the ack."""

    def __init__(self, inner: Gateway, queue: asyncio.Queue[PendingTurn]) -> None:
        self._inner = inner
        self._queue = queue

    def inbound(self) -> AsyncIterator[InboundUpdate]:
        return self._inner.inbound()

    async def ack(self, update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None:
        await self._inner.ack(update, event_id=event_id)  # durable BEFORE anything else
        wants_turn = (
            update.kind is UpdateKind.TEXT and not update.untrusted and event_id is not None
        ) or update.kind is UpdateKind.CALLBACK
        if wants_turn:
            await self._queue.put(PendingTurn(update, event_id))  # back-pressure, after the ack

    async def send_text(self, text: str, *, reply_to: MessageRef | None = None) -> list[MessageRef]:
        return await self._inner.send_text(text, reply_to=reply_to)

    async def send_approval(self, card: ApprovalCard) -> MessageRef:
        return await self._inner.send_approval(card)

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None:
        await self._inner.close_approval(ref, outcome=outcome)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self._inner.answer_callback(callback_id, text)

    async def fetch_file(self, file: FileRef, *, max_bytes: int) -> bytes:
        return await self._inner.fetch_file(file, max_bytes=max_bytes)


def _remember(result: TurnResult) -> list[ChatMessage]:
    kept = [m for m in result.messages if m.role != "system"]
    return [*kept, ChatMessage(role="assistant", content=result.text)]


class History:
    """Short in-process conversation window. Always starts at a user message so
    a trimmed window never begins with an orphan tool result."""

    def __init__(self, max_messages: int) -> None:
        self._max = max_messages
        self._msgs: deque[ChatMessage] = deque()

    def get(self) -> list[ChatMessage]:
        return list(self._msgs)

    def replace(self, messages: list[ChatMessage]) -> None:
        if self._max == 0:
            self._msgs.clear()
            return
        window = messages[-self._max :]
        while window and window[0].role != "user":
            window = window[1:]
        self._msgs = deque(window)


class TurnWorker:
    def __init__(
        self,
        gateway: Gateway,
        orchestrator: Orchestrator,
        queue: asyncio.Queue[PendingTurn],
        *,
        history_messages: int,
        turn_timeout_s: float = 180.0,
    ) -> None:
        self._gw = gateway
        self._orch = orchestrator
        self._queue = queue
        self._history = History(history_messages)
        self._timeout = turn_timeout_s

    async def serve(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await asyncio.wait_for(self._handle(item), timeout=self._timeout)
            except TelegramAuthError:
                raise  # revoked token: not retryable, surfaces as a fatal loop error
            except Exception as exc:  # noqa: BLE001 - one bad turn must not stop the loop
                log.warning("turn failed err=%s", type(exc).__name__)
                await self._apologise(item)
            finally:
                self._queue.task_done()

    async def _apologise(self, item: PendingTurn) -> None:
        if item.update.kind is not UpdateKind.TEXT:
            return
        try:
            await self._gw.send_text(TURN_FAILED_MESSAGE, reply_to=_ref(item.update))
        except TelegramAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("turn apology failed err=%s", type(exc).__name__)

    async def _handle(self, item: PendingTurn) -> None:
        u = item.update
        if u.kind is UpdateKind.CALLBACK:
            # No approval can be pending before P3.2: say so instead of leaving a spinner.
            if u.callback_id:
                await self._gw.answer_callback(u.callback_id, STALE_CALLBACK_MESSAGE)
            return
        text, _ = clean_text(u.text or "", max_chars=MAX_TURN_TEXT_CHARS)
        if not text.strip():
            return
        result = await self._orch.run_turn(text, history=self._history.get(), turn_id=uuid.uuid4())
        self._history.replace(_remember(result))
        await self._gw.send_text(result.text, reply_to=_ref(u))


def _ref(u: InboundUpdate) -> MessageRef | None:
    return MessageRef(u.message_id) if u.message_id is not None else None
