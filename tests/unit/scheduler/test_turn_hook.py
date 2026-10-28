# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Post-turn hook (TARGET_DESIGN 4.1 step 7): a trusted user text turn enqueues
memory extraction for ITS event; nothing else does; a failing hook never breaks
the turn."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from src.app.fakes import InMemoryGateway
from src.app.pipeline import PendingTurn, TurnWorker
from src.gateway.types import InboundUpdate, UpdateKind
from src.orchestrator.loop import TurnResult


class Orch:
    def __init__(self, status: str = "ok") -> None:
        self.status = status

    async def run_turn(self, text: str, **_: Any) -> TurnResult:
        return TurnResult("answer", self.status, messages=())


def upd(
    uid: int, kind: UpdateKind = UpdateKind.TEXT, text: str | None = "hello there"
) -> InboundUpdate:
    return InboundUpdate(update_id=uid, kind=kind, message_id=uid, text=text)


async def drain(worker: TurnWorker, q: asyncio.Queue[PendingTurn]) -> None:
    task = asyncio.create_task(worker.serve())
    await asyncio.wait_for(q.join(), 5)
    task.cancel()


def make(
    hook: Any, orch: Any = None
) -> tuple[TurnWorker, asyncio.Queue[PendingTurn], InMemoryGateway]:
    gw = InMemoryGateway()
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch or Orch(), q, history_messages=4, on_turn_done=hook)
    return worker, q, gw


async def test_text_turn_enqueues_extraction_for_its_event() -> None:
    seen: list[uuid.UUID] = []

    async def hook(event_id: uuid.UUID) -> None:
        seen.append(event_id)

    worker, q, gw = make(hook)
    eid = uuid.uuid4()
    q.put_nowait(PendingTurn(upd(1), eid))
    await drain(worker, q)
    assert seen == [eid] and gw.sent == ["answer"]


async def test_callbacks_and_missing_event_ids_do_not_enqueue() -> None:
    seen: list[uuid.UUID] = []

    async def hook(event_id: uuid.UUID) -> None:
        seen.append(event_id)

    worker, q, _ = make(hook)
    q.put_nowait(PendingTurn(upd(1, UpdateKind.CALLBACK, None), uuid.uuid4()))
    q.put_nowait(PendingTurn(upd(2), None))
    await drain(worker, q)
    assert seen == []


async def test_a_raising_hook_does_not_break_the_turn() -> None:
    async def hook(event_id: uuid.UUID) -> None:
        raise RuntimeError("db down")

    worker, q, gw = make(hook)
    q.put_nowait(PendingTurn(upd(1), uuid.uuid4()))
    await drain(worker, q)
    assert gw.sent == ["answer"]  # the reply still went out


async def test_tools_see_the_turns_event_id_and_only_during_the_turn() -> None:
    from src.core.turn_context import current_event_id

    seen: list[uuid.UUID | None] = []

    class Spy(Orch):
        async def run_turn(self, text: str, **kw: Any) -> TurnResult:
            seen.append(current_event_id())
            return await super().run_turn(text, **kw)

    worker, q, _ = make(None, Spy())
    eid = uuid.uuid4()
    q.put_nowait(PendingTurn(upd(1), eid))
    await drain(worker, q)
    assert seen == [eid] and current_event_id() is None
