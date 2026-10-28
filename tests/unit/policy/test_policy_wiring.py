# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.1 wiring: the dispatcher builds the PolicyContext from code facts (mode,
rate counter, scopes, exposure, taint), and the turn worker carries taint
across turns (ADR-0010)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from src.app.fakes import InMemoryGateway
from src.app.pipeline import PendingTurn, TurnWorker
from src.gateway.types import InboundUpdate, UpdateKind
from src.llm.types import LLMMode, ToolSpec
from src.orchestrator.dispatch import Dispatcher, ExecutionCounter, ToolOutput
from src.orchestrator.loop import TurnResult
from src.policy import PolicyContext, PolicyEngine, PolicyMode, ProposedCall, Risk
from src.policy.registry import READ_PER_HOUR, WRITE_PER_HOUR


class Spy:
    def __init__(self, name: str, risk: Risk) -> None:
        self.spec = ToolSpec(name=name, description=name)
        self.risk = risk
        self.runs = 0

    async def run(self, args: Mapping[str, Any]) -> ToolOutput:
        self.runs += 1
        return ToolOutput("ok", untrusted=False)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def mode_of(value: Any) -> Any:
    async def src() -> Any:
        if isinstance(value, BaseException):
            raise value
        return value

    return src


ON = frozenset({"tasks_list", "tasks_update"})


@pytest.mark.parametrize(
    ("mode", "read_runs", "write_runs", "write_rule"),
    [
        (LLMMode.NORMAL, 1, 1, "allow.write_internal"),
        (LLMMode.READ_ONLY, 1, 0, "deny.read_only"),
        (LLMMode.KILLED, 1, 0, "deny.killed"),  # local READ survives the kill switch
        ("normal", 1, 1, "allow.write_internal"),
        ("bogus", 1, 0, "deny.killed"),  # unknown mode fails closed to KILLED
        (None, 1, 0, "deny.killed"),
        (RuntimeError("db down"), 1, 0, "deny.killed"),
    ],
)
async def test_guard_mode_feeds_the_policy(
    mode: Any, read_runs: int, write_runs: int, write_rule: str
) -> None:
    r, w = Spy("tasks_list", Risk.READ), Spy("tasks_update", Risk.WRITE_INTERNAL)
    d = Dispatcher({"tasks_list": r, "tasks_update": w}, PolicyEngine(), mode_source=mode_of(mode))
    await d.dispatch("tasks_list", {}, exposed=ON)
    res = await d.dispatch("tasks_update", {}, exposed=ON)
    assert (r.runs, w.runs, res.rule_id) == (read_runs, write_runs, write_rule)


async def test_rate_limit_counts_executions_per_tool_per_hour() -> None:
    clock = Clock()
    w = Spy("tasks_update", Risk.WRITE_INTERNAL)
    d = Dispatcher({"tasks_update": w}, PolicyEngine(), counter=ExecutionCounter(clock))
    for _ in range(WRITE_PER_HOUR):
        assert (await d.dispatch("tasks_update", {}, exposed=ON)).status == "ok"
    res = await d.dispatch("tasks_update", {}, exposed=ON)
    assert (res.status, res.rule_id, w.runs) == ("denied", "deny.rate_limit", WRITE_PER_HOUR)
    clock.t += 3600.0
    assert (await d.dispatch("tasks_update", {}, exposed=ON)).status == "ok"


async def test_denied_calls_do_not_consume_the_rate_budget() -> None:
    clock = Clock()
    r = Spy("tasks_list", Risk.READ)
    d = Dispatcher({"tasks_list": r}, PolicyEngine(), counter=ExecutionCounter(clock))
    for _ in range(READ_PER_HOUR * 2):
        await d.dispatch("tasks_list", {}, exposed=frozenset())  # unexposed: denied
    assert (await d.dispatch("tasks_list", {}, exposed=ON)).status == "ok"


def test_counter_is_bounded_and_ignores_unknown() -> None:
    c = ExecutionCounter(Clock(), limit=3)
    for _ in range(10):
        c.record("t")
    assert c.count("t") == 3 and c.count("never") == 0


class Recorder:
    def __init__(self) -> None:
        self.ctx: list[PolicyContext] = []
        self.calls: list[ProposedCall] = []

    def decide(self, call: ProposedCall, ctx: PolicyContext) -> Any:
        self.calls.append(call)
        self.ctx.append(ctx)
        return PolicyEngine().decide(call, ctx)


@pytest.mark.parametrize("tainted", [False, True, None, 0, "no"])
async def test_ctx_is_built_from_code_facts_only(tainted: Any) -> None:
    rec = Recorder()
    w = Spy("tasks_update", Risk.WRITE_INTERNAL)
    d = Dispatcher({"tasks_update": w}, rec, scopes_enabled=frozenset({"tasks"}))
    args = {"user_confirmed": True, "tainted": False, "exposed": True, "calls_last_hour": 0}
    await d.dispatch("tasks_update", args, exposed=ON, tainted=tainted)
    x = rec.ctx[0]
    assert x.tainted is (tainted is not False)
    assert (x.exposed, x.user_confirmed, x.step_up_confirmed) == (True, False, False)
    assert x.scopes_enabled == frozenset({"tasks"}) and x.mode is PolicyMode.NORMAL
    assert rec.calls[0].risk is Risk.WRITE_INTERNAL  # from the tool's code, not args


@pytest.mark.parametrize(
    ("risk", "recipient", "destructive"),
    [
        (Risk.READ, False, False),
        (Risk.WRITE_INTERNAL, False, False),
        (Risk.WRITE_EXTERNAL, True, False),  # no contact allowlist yet: fail closed
        (Risk.DESTRUCTIVE, False, True),
    ],
)
async def test_ctx_effect_flags_derive_from_tool_risk(
    risk: Risk, recipient: bool, destructive: bool
) -> None:
    rec = Recorder()
    d = Dispatcher({"t": Spy("t", risk)}, rec)
    await d.dispatch("t", {}, exposed=frozenset({"t"}))
    assert (rec.ctx[0].external_recipient, rec.ctx[0].destructive) == (recipient, destructive)


async def test_unknown_and_unexposed_tools_never_run() -> None:
    rec = Recorder()
    t = Spy("tasks_list", Risk.READ)
    d = Dispatcher({"tasks_list": t}, rec)
    a = await d.dispatch("nope", {}, exposed=frozenset({"nope"}))
    b = await d.dispatch("tasks_list", {}, exposed=frozenset())
    assert (a.rule_id, b.rule_id, t.runs) == ("deny.unknown_tool", "deny.unexposed", 0)
    assert rec.calls[0].risk is None and rec.ctx[1].exposed is False


async def test_require_approval_reports_approval_unavailable() -> None:
    w = Spy("tasks_delete", Risk.WRITE_INTERNAL)
    d = Dispatcher({"tasks_delete": w}, PolicyEngine())
    res = await d.dispatch("tasks_delete", {}, exposed=frozenset({"tasks_delete"}))
    assert w.runs == 0 and res.status == "denied"
    assert (res.needs_approval, res.step_up, res.rule_id) == (True, True, "approval.destructive")
    assert "approval_unavailable" in res.content


async def test_non_string_tool_name_is_denied() -> None:
    d = Dispatcher({}, PolicyEngine())
    res = await d.dispatch(123, {}, exposed=frozenset())  # type: ignore[arg-type]
    assert res.status == "denied"


# ----------------------------------------------------------------- cross-turn carry


class TaintStub:
    def __init__(self, own: list[bool | Exception]) -> None:
        self.own = own
        self.carried: list[bool] = []

    async def run_turn(self, text: str, *, carried_taint: bool = False, **_: Any) -> TurnResult:
        self.carried.append(carried_taint)
        nxt = self.own.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return TurnResult("reply", "ok", tainted=nxt)


async def _serve(orch: TaintStub, n: int, history_messages: int) -> None:
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    w = TurnWorker(InMemoryGateway(), orch, q, history_messages=history_messages)  # type: ignore[arg-type]
    for i in range(n):
        u = InboundUpdate(update_id=i, kind=UpdateKind.TEXT, message_id=i, text=f"t{i}")
        await q.put(PendingTurn(u, uuid.uuid4()))
    task = asyncio.create_task(w.serve())
    try:
        await asyncio.wait_for(q.join(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_tainted_turn_taints_the_next_three() -> None:
    orch = TaintStub([True, False, False, False, False])
    await _serve(orch, 5, history_messages=6)
    assert orch.carried == [False, True, True, True, False]


async def test_carry_lasts_until_the_reply_leaves_a_long_history() -> None:
    orch = TaintStub([True] + [False] * 6)
    await _serve(orch, 7, history_messages=10)  # 5 kept turns to evict
    assert orch.carried == [False, True, True, True, True, True, False]


async def test_failed_turn_arms_the_carry() -> None:
    orch = TaintStub([RuntimeError("x"), False, False, False, False])
    await _serve(orch, 5, history_messages=0)
    assert orch.carried == [False, True, True, True, False]


async def test_orchestrator_feeds_the_guard_mode_to_the_dispatcher() -> None:
    """A spend cap tripped mid-turn stops later writes in that turn."""
    from src.llm.fake import CompromisedFakeLLM
    from src.orchestrator.loop import Orchestrator
    from tests.fakes.llm import build_env

    env = build_env(provider=CompromisedFakeLLM())

    class ReadOnlyAfterChat:
        async def chat(self, req: Any) -> Any:
            return await env.llm.chat(req)

        async def mode(self) -> LLMMode:
            return LLMMode.READ_ONLY

    w = Spy("tasks_update", Risk.WRITE_INTERNAL)
    o = Orchestrator(ReadOnlyAfterChat(), [w], PolicyEngine(), character_card="c")  # type: ignore[arg-type]
    r = await o.run_turn('tasks_update {"task_id": "1"}')
    assert w.runs == 0 and r.tool_events[0].rule_id == "deny.read_only"
