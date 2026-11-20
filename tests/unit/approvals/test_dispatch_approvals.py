# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P3.2 dispatcher wiring (no DB): `require_approval` asks the requester and
never runs; `execute_approved` is the only path to a confirmed context and only
for a sealed, hash-consistent `ConsumedApproval`; every decision is audited
before a tool runs and an audit failure stops the tool."""

from __future__ import annotations

import types
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from src.approvals.canonical import payload_sha256
from src.approvals.types import ApprovalAsk, AskOutcome, AuditRecord, seal_consumed
from src.llm.types import ToolSpec
from src.orchestrator.dispatch import Dispatcher, ToolOutput
from src.policy import Decision, PolicyContext, PolicyEngine, ProposedCall, Risk


class Spy:
    def __init__(self, name: str, risk: Risk) -> None:
        self.spec = ToolSpec(name=name, description=name)
        self.risk = risk
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: Mapping[str, Any]) -> ToolOutput:
        self.calls.append(dict(args))
        return ToolOutput("ok", untrusted=False)


class Requester:
    def __init__(self, outcome: Any = AskOutcome.REQUESTED, exc: Exception | None = None) -> None:
        self.outcome = outcome
        self.exc = exc
        self.asks: list[ApprovalAsk] = []

    async def request(self, ask: ApprovalAsk) -> AskOutcome:
        self.asks.append(ask)
        if self.exc:
            raise self.exc
        return self.outcome  # type: ignore[no-any-return]


class Audit:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.recs: list[AuditRecord] = []

    async def audit(self, rec: AuditRecord) -> None:
        if self.fail:
            raise ConnectionError("db down")
        self.recs.append(rec)


class Recorder(PolicyEngine):
    def __init__(self) -> None:
        super().__init__()
        self.ctx: list[PolicyContext] = []

    def decide(self, call: ProposedCall, ctx: PolicyContext) -> Decision:
        self.ctx.append(ctx)
        return super().decide(call, ctx)


def tools() -> dict[str, Spy]:
    return {
        "tasks_update": Spy("tasks_update", Risk.WRITE_INTERNAL),
        "tasks_delete": Spy("tasks_delete", Risk.WRITE_INTERNAL),
        "tasks_list": Spy("tasks_list", Risk.READ),
    }


def consumed(tool: str, args: dict[str, Any], *, step_up: bool = False, **over: Any) -> Any:
    kw: dict[str, Any] = {
        "action_id": uuid.uuid4(),
        "tool": tool,
        "action": "call",
        "payload": args,
        "payload_sha256": payload_sha256(tool, "call", args),
        "step_up_verified": step_up,
        "source_event_id": None,
    }
    kw.update(over)
    return seal_consumed(**kw)


ALL = frozenset({"tasks_update", "tasks_delete", "tasks_list"})


@pytest.mark.parametrize("outcome", list(AskOutcome))
async def test_require_approval_asks_and_never_runs(outcome: AskOutcome) -> None:
    t = tools()
    req = Requester(outcome)
    d = Dispatcher(t, PolicyEngine(), approvals=req)
    res = await d.dispatch("tasks_update", {"task_id": "1"}, exposed=ALL, tainted=True)
    assert t["tasks_update"].calls == [] and res.status == "denied" and res.needs_approval
    assert res.approval == outcome.value and "approval.write_internal_tainted" in res.content
    (ask,) = req.asks
    assert (ask.tool, dict(ask.args), ask.step_up, ask.tainted) == (
        "tasks_update",
        {"task_id": "1"},
        False,
        True,
    )


async def test_step_up_flag_is_passed_to_the_requester() -> None:
    req = Requester()
    d = Dispatcher(tools(), PolicyEngine(), approvals=req)
    await d.dispatch("tasks_delete", {"task_id": "1"}, exposed=ALL)
    assert req.asks[0].step_up is True and req.asks[0].rule_id == "approval.destructive"


@pytest.mark.parametrize(
    "req", [Requester(exc=RuntimeError("x")), Requester(outcome="requested"), Requester(None)]
)
async def test_broken_requester_is_a_denial(req: Requester) -> None:
    t = tools()
    d = Dispatcher(t, PolicyEngine(), approvals=req)
    res = await d.dispatch("tasks_update", {}, exposed=ALL, tainted=True)
    assert res.approval == "failed" and t["tasks_update"].calls == []


async def test_allow_and_deny_do_not_ask() -> None:
    req = Requester()
    d = Dispatcher(tools(), PolicyEngine(), approvals=req)
    await d.dispatch("tasks_list", {}, exposed=ALL)
    await d.dispatch("nope", {}, exposed=ALL)
    assert req.asks == []


async def test_every_decision_is_audited_before_the_run_without_values() -> None:
    t = tools()
    audit = Audit()
    d = Dispatcher(t, PolicyEngine(), audit=audit)
    canary = "CANARY-dispatch-audit"
    await d.dispatch("tasks_list", {"q": canary}, exposed=ALL)
    await d.dispatch("tasks_update", {"q": canary}, exposed=ALL, tainted=True)
    await d.dispatch("ghost", {"q": canary}, exposed=ALL)
    assert [(r.tool, r.rule_id, r.outcome, r.executed) for r in audit.recs] == [
        ("tasks_list", "allow.read", "allow", True),
        ("tasks_update", "approval.write_internal_tainted", "require_approval", False),
        ("ghost", "deny.unknown_tool", "deny", False),
    ]
    assert canary not in repr(audit.recs)


@pytest.mark.parametrize("name", ["tasks_list", "tasks_update"])
async def test_audit_failure_means_nothing_runs(name: str) -> None:
    """Mutation proof for fail-closed audit: ignoring the sink failure runs the tool."""
    t = tools()
    d = Dispatcher(t, PolicyEngine(), audit=Audit(fail=True))
    res = await d.dispatch(name, {}, exposed=ALL, tainted=False)
    assert res.rule_id == "deny.audit_unavailable" and t[name].calls == []


async def test_execute_approved_runs_exactly_the_stored_args_with_a_confirmed_context() -> None:
    t = tools()
    pol = Recorder()
    d = Dispatcher(t, pol)
    res = await d.execute_approved(consumed("tasks_update", {"task_id": "7", "tags": ["a"]}))
    assert res.status == "ok" and res.rule_id == "allow.confirmed"
    assert t["tasks_update"].calls == [{"task_id": "7", "tags": ["a"]}]
    (ctx,) = pol.ctx
    assert (ctx.user_confirmed, ctx.step_up_confirmed, ctx.tainted, ctx.exposed) == (
        True,
        False,
        True,
        True,
    )


@pytest.mark.parametrize(("step_up", "runs"), [(False, 0), (True, 1)])
async def test_destructive_needs_the_verified_step_up(step_up: bool, runs: int) -> None:
    t = tools()
    d = Dispatcher(t, PolicyEngine())
    res = await d.execute_approved(consumed("tasks_delete", {"task_id": "1"}, step_up=step_up))
    assert len(t["tasks_delete"].calls) == runs
    if not runs:
        assert res.rule_id == "approval.destructive" and res.status == "denied"


def _fake_like_consumed() -> Any:
    return types.SimpleNamespace(
        action_id=uuid.uuid4(),
        tool="tasks_update",
        action="call",
        payload={},
        payload_sha256=payload_sha256("tasks_update", "call", {}),
        step_up_verified=True,
        source_event_id=None,
    )


@pytest.mark.parametrize(
    ("label", "build", "rule"),
    [
        ("duck_typed", _fake_like_consumed, "deny.error"),
        ("dict", lambda: {"tool": "tasks_update", "action": "call"}, "deny.error"),
        ("none", lambda: None, "deny.error"),
        (
            "hash_mismatch",
            lambda: consumed("tasks_update", {"a": 1}, payload_sha256="0" * 64),
            "deny.approval_mismatch",
        ),
        (
            "promote_action",
            lambda: consumed(
                "memory",
                {"item_id": "x"},
                action="promote",
                payload_sha256=payload_sha256("memory", "promote", {"item_id": "x"}),
            ),
            "deny.error",
        ),
        ("unknown_tool", lambda: consumed("tasks_purge_all", {}), "deny.unknown_tool"),
        (
            "unstorable",
            lambda: consumed("tasks_update", {"a": 1}, payload={"a": float("nan")}),
            "deny.approval_mismatch",
        ),
    ],
)
async def test_execute_approved_refuses_anything_but_a_consistent_sealed_approval(
    label: str, build: Any, rule: str
) -> None:
    t = tools()
    res = await Dispatcher(t, PolicyEngine()).execute_approved(build())
    assert res.status == "denied" and res.rule_id == rule, label
    assert all(not s.calls for s in t.values())


async def test_execute_approved_is_audited_with_the_action_id() -> None:
    audit = Audit()
    c = consumed("tasks_update", {})
    await Dispatcher(tools(), PolicyEngine(), audit=audit).execute_approved(c)
    (rec,) = audit.recs
    assert (rec.event, rec.action_id, rec.executed) == ("decided_confirmed", c.action_id, True)


async def test_model_path_is_never_confirmed_even_after_an_approval_ran() -> None:
    t = tools()
    pol = Recorder()
    d = Dispatcher(t, pol, approvals=Requester())
    await d.execute_approved(consumed("tasks_update", {"task_id": "1"}))
    await d.dispatch("tasks_update", {"task_id": "1"}, exposed=ALL, tainted=True)
    assert [c.user_confirmed for c in pol.ctx] == [True, False]
    assert len(t["tasks_update"].calls) == 1


async def test_context_failure_is_a_denial() -> None:
    t = tools()
    d = Dispatcher(t, PolicyEngine())

    async def bad_context(*a: Any, **k: Any) -> Any:
        raise RuntimeError("x")

    d._context = bad_context  # type: ignore[method-assign]
    res = await d.dispatch("tasks_list", {}, exposed=ALL)
    assert res.rule_id == "deny.error" and t["tasks_list"].calls == []
    res = await d.execute_approved(consumed("tasks_update", {}))
    assert res.rule_id == "deny.error" and t["tasks_update"].calls == []
