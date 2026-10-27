# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Turn plumbing: ack before turn, who gets a turn, failure handling, history."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from src.app.fakes import InMemoryGateway
from src.app.pipeline import (
    STALE_CALLBACK_MESSAGE,
    TURN_FAILED_MESSAGE,
    History,
    PendingTurn,
    TurnGateway,
    TurnWorker,
)
from src.gateway.types import InboundUpdate, TelegramAuthError, UpdateKind
from src.llm.types import ChatMessage
from src.orchestrator.loop import TurnResult


def text_update(uid: int, text: str = "hi", *, untrusted: bool = False) -> InboundUpdate:
    return InboundUpdate(
        update_id=uid, kind=UpdateKind.TEXT, message_id=uid * 10, text=text, untrusted=untrusted
    )


class StubOrchestrator:
    def __init__(self, reply: str = "answer", *, boom: BaseException | None = None) -> None:
        self.calls: list[tuple[str, list[ChatMessage]]] = []
        self.reply = reply
        self.boom = boom

    async def run_turn(self, text: str, *, history: Any = (), **_: Any) -> TurnResult:
        self.calls.append((text, list(history)))
        if self.boom is not None:
            raise self.boom
        msgs = (
            ChatMessage(role="system", content="card"),
            *history,
            ChatMessage(role="user", content=text),
        )
        return TurnResult(self.reply, "ok", messages=msgs)


async def test_turn_is_queued_only_after_the_ack_is_durable() -> None:
    order: list[str] = []
    inner = InMemoryGateway()
    real_ack = inner.ack

    async def slow_ack(update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None:
        order.append("ack-start")
        await asyncio.sleep(0.01)
        await real_ack(update, event_id=event_id)
        order.append("ack-done")

    inner.ack = slow_ack  # type: ignore[method-assign]
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    gw = TurnGateway(inner, q)
    await gw.ack(text_update(1), event_id=uuid.uuid4())
    order.append(f"queued={q.qsize()}")
    assert order == ["ack-start", "ack-done", "queued=1"]


async def test_failed_ack_queues_nothing() -> None:
    inner = InMemoryGateway()

    async def bad_ack(update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None:
        raise RuntimeError("db down")

    inner.ack = bad_ack  # type: ignore[method-assign]
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    with pytest.raises(RuntimeError):
        await TurnGateway(inner, q).ack(text_update(1), event_id=uuid.uuid4())
    assert q.empty()


@pytest.mark.parametrize(
    ("update", "event_id", "queued"),
    [
        (text_update(1), uuid.uuid4(), True),
        (text_update(2), None, False),  # blank text: no event, no turn
        (text_update(3, untrusted=True), uuid.uuid4(), False),  # INV-6: forwards never instruct
        (InboundUpdate(update_id=4, kind=UpdateKind.VOICE), uuid.uuid4(), False),
        (InboundUpdate(update_id=5, kind=UpdateKind.DOCUMENT), uuid.uuid4(), False),
        (InboundUpdate(update_id=6, kind=UpdateKind.CALLBACK, callback_id="c"), None, True),
    ],
)
async def test_who_gets_a_turn(
    update: InboundUpdate, event_id: uuid.UUID | None, queued: bool
) -> None:
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    await TurnGateway(InMemoryGateway(), q).ack(update, event_id=event_id)
    assert (q.qsize() == 1) is queued


async def _run_one(worker: TurnWorker, q: asyncio.Queue[PendingTurn], item: PendingTurn) -> None:
    await q.put(item)
    task = asyncio.create_task(worker.serve())
    try:
        await asyncio.wait_for(q.join(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_text_turn_replies_and_keeps_history() -> None:
    gw, orch = InMemoryGateway(), StubOrchestrator()
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch, q, history_messages=20)  # type: ignore[arg-type]
    await _run_one(worker, q, PendingTurn(text_update(1, "first"), uuid.uuid4()))
    await _run_one(worker, q, PendingTurn(text_update(2, "second"), uuid.uuid4()))
    assert gw.sent == ["answer", "answer"]
    assert orch.calls[0][1] == []
    assert [m.content for m in orch.calls[1][1]] == ["first", "answer"]  # prior turn remembered


async def test_failed_turn_apologises_once_and_the_worker_survives() -> None:
    gw, orch = InMemoryGateway(), StubOrchestrator(boom=RuntimeError("SECRET-in-message"))
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch, q, history_messages=4)  # type: ignore[arg-type]
    await _run_one(worker, q, PendingTurn(text_update(1), uuid.uuid4()))
    assert gw.sent == [TURN_FAILED_MESSAGE]
    orch.boom = None
    await _run_one(worker, q, PendingTurn(text_update(2), uuid.uuid4()))
    assert gw.sent[-1] == "answer"


async def test_turn_timeout_is_a_failed_turn_not_a_hang() -> None:
    class Slow(StubOrchestrator):
        async def run_turn(self, text: str, **_: Any) -> TurnResult:
            await asyncio.sleep(60)
            raise AssertionError

    gw = InMemoryGateway()
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, Slow(), q, history_messages=4, turn_timeout_s=0.05)  # type: ignore[arg-type]
    await _run_one(worker, q, PendingTurn(text_update(1), uuid.uuid4()))
    assert gw.sent == [TURN_FAILED_MESSAGE]


async def test_revoked_token_during_a_turn_is_fatal_not_swallowed() -> None:
    gw = InMemoryGateway()
    orch = StubOrchestrator(boom=TelegramAuthError("unauthorized", code=401))
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch, q, history_messages=4)  # type: ignore[arg-type]
    await q.put(PendingTurn(text_update(1), uuid.uuid4()))
    with pytest.raises(TelegramAuthError):
        await asyncio.wait_for(worker.serve(), 5)


async def test_callback_is_answered_with_a_fixed_text_and_runs_no_turn() -> None:
    gw, orch = InMemoryGateway(), StubOrchestrator()
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch, q, history_messages=4)  # type: ignore[arg-type]
    cb = InboundUpdate(update_id=9, kind=UpdateKind.CALLBACK, callback_id="cb9", callback_data="x")
    await _run_one(worker, q, PendingTurn(cb, None))
    assert gw.callbacks == [("cb9", STALE_CALLBACK_MESSAGE)] and orch.calls == []


def test_history_window_never_starts_with_an_orphan_tool_message() -> None:
    h = History(3)
    msgs = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="tool", content="t1", tool_call_id="x"),
        ChatMessage(role="assistant", content="a2"),
    ]
    h.replace(msgs)  # last 3 = [assistant, tool, assistant]: no user -> dropped to empty
    assert h.get() == []
    h.replace([*msgs, ChatMessage(role="user", content="u2")])
    assert [m.content for m in h.get()] == ["u2"]
    History(0).replace(msgs)


# ---- MED-2: a blocked/failed turn must never poison later turns ----

CANARY = "CANARY-91ac33e7f02b4d58"  # pragma: allowlist secret


class RecordingStub(StubOrchestrator):
    def __init__(self, status: str = "ok") -> None:
        super().__init__()
        self.status = status

    async def run_turn(self, text: str, *, history: Any = (), **_: Any) -> TurnResult:
        self.calls.append((text, list(history)))
        msgs = (
            ChatMessage(role="system", content="SYSTEM-PROMPT-WITH-CARD"),
            *history,
            ChatMessage(role="user", content=text),
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="tool", content="<<<DATA untrusted tool output>>>", tool_call_id="t"),
        )
        return TurnResult(self.reply, self.status, messages=msgs)


@pytest.mark.parametrize("status", ["unavailable", "budget_exceeded", "error"])
async def test_non_ok_turns_are_not_remembered(status: str) -> None:
    gw, orch = InMemoryGateway(), RecordingStub(status)
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch, q, history_messages=20)  # type: ignore[arg-type]
    await _run_one(worker, q, PendingTurn(text_update(1, f"my key is {CANARY}"), uuid.uuid4()))
    orch.status = "ok"
    await _run_one(worker, q, PendingTurn(text_update(2, "clean question"), uuid.uuid4()))
    assert orch.calls[1][1] == []  # nothing from the failed turn reached the next request


async def test_history_holds_only_user_and_assistant_text_never_system_or_tool() -> None:
    gw, orch = InMemoryGateway(), RecordingStub("ok")
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch, q, history_messages=20)  # type: ignore[arg-type]
    await _run_one(worker, q, PendingTurn(text_update(1, "first"), uuid.uuid4()))
    await _run_one(worker, q, PendingTurn(text_update(2, "second"), uuid.uuid4()))
    hist = orch.calls[1][1]
    assert [(m.role, m.content) for m in hist] == [("user", "first"), ("assistant", "answer")]
    assert all("SYSTEM-PROMPT" not in (m.content or "") for m in hist)


async def test_canary_message_blocks_only_itself_provider_runs_for_the_next_clean_turn() -> None:
    """End to end through the real guard: the turn with a canary is refused before
    any provider call; the next, clean turn reaches the provider and its request
    contains no trace of the canary."""
    from src.llm.fake import FakeLLM
    from src.orchestrator.dispatch import DenyAllWritesPolicy
    from src.orchestrator.loop import Orchestrator
    from tests.fakes.llm import build_env

    provider = FakeLLM(default="fine")
    env = build_env(provider=provider, canaries=(CANARY,))
    orch = Orchestrator(env.llm, [], DenyAllWritesPolicy(), character_card="card")
    gw = InMemoryGateway()
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch, q, history_messages=20)
    await _run_one(worker, q, PendingTurn(text_update(1, f"here: {CANARY}"), uuid.uuid4()))
    assert provider.requests == []  # blocked before any provider call
    await _run_one(worker, q, PendingTurn(text_update(2, "what is 2+2?"), uuid.uuid4()))
    assert len(provider.requests) == 1  # NOT blocked by a poisoned history
    sent_text = " ".join(m.content or "" for m in provider.requests[0].messages)
    assert CANARY not in sent_text
    await _run_one(worker, q, PendingTurn(text_update(3, "and 3+3?"), uuid.uuid4()))
    assert len(provider.requests) == 2 and gw.sent[-1] == "fine"


async def test_on_progress_fires_after_each_turn_even_a_failed_one() -> None:
    beats: list[int] = []
    gw, orch = InMemoryGateway(), StubOrchestrator(boom=RuntimeError("x"))
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    worker = TurnWorker(gw, orch, q, history_messages=4, on_progress=lambda: beats.append(1))  # type: ignore[arg-type]
    await _run_one(worker, q, PendingTurn(text_update(1), uuid.uuid4()))
    assert beats == [1]
