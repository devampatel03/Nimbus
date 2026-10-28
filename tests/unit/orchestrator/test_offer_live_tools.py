# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Only tools that could run are offered to the model: unbound groups and tools
whose policy scope is not enabled are hidden, so they cannot crowd live
tasks/reminders tools out of the 12-tool cap (R16). Hidden tools stay denied
(`deny.unexposed`). No scope information means local scopes only (fail closed)."""

from __future__ import annotations

import pytest

from src.llm.fake import FakeLLM
from src.llm.types import ProviderResponse, ToolCallRequest, ToolSpec, Usage
from src.orchestrator.dispatch import DenyAllWritesPolicy, Risk, ToolOutput
from src.orchestrator.loop import Orchestrator
from src.policy import TOOL_RULES, PolicyConfig, PolicyEngine, ToolRule
from src.policy.registry import READ_PER_HOUR
from src.tools.base import FunctionTool, ToolGroup, object_schema
from src.tools.integrations.reminders import RemindersTool
from src.tools.integrations.tasks import TasksTool
from tests.fakes.llm import build_env

N_EXTRA = 8
MESSAGES = ["hi", "remind me to call mom at 5", "cancel the dentist"]


class _Stub(ToolGroup):
    name = "stub"

    def __init__(self, bound: bool = False) -> None:
        self.bound = bound

    async def _run(self, args: object) -> ToolOutput:
        return ToolOutput("x")

    def tools(self):  # type: ignore[no-untyped-def]
        ts = [
            FunctionTool(f"cal_{i}", "calendar event", Risk.READ, object_schema({}), self._run)
            for i in range(N_EXTRA)
        ]
        return self.gate(ts, lambda: self.bound)


def _live() -> tuple[list, set[str]]:  # type: ignore[type-arg]
    rem, tasks = RemindersTool(), TasksTool()
    rem._store, rem._clock = object(), object()  # type: ignore[assignment]
    tasks._sf, tasks._clock = object(), object()  # type: ignore[assignment]
    tools = [*rem.tools(), *tasks.tools()]
    return tools, {t.spec.name for t in tools}


def _orch(tools, fake: FakeLLM, **kw) -> Orchestrator:  # type: ignore[no-untyped-def]
    return Orchestrator(
        build_env(provider=fake).llm, tools, DenyAllWritesPolicy(), character_card="c", **kw
    )


def _offered(fake: FakeLLM) -> set[str]:
    return {t.name for t in fake.requests[0].tools}


@pytest.mark.parametrize("msg", MESSAGES)
async def test_unbound_extra_tools_do_not_displace_live_tools(msg: str) -> None:
    live, live_names = _live()
    fake = FakeLLM(["ok"])
    await _orch([*live, *_Stub(bound=False).tools()], fake).run_turn(msg)
    assert _offered(fake) == live_names  # all 11, none of the 8 unbound


@pytest.mark.parametrize("msg", MESSAGES)
async def test_scope_disabled_tools_are_hidden_until_scope_enabled(
    msg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = dict(TOOL_RULES) | {
        f"cal_{i}": ToolRule(Risk.READ, "calendar", READ_PER_HOUR, external_service=True)
        for i in range(N_EXTRA)
    }
    monkeypatch.setattr("src.orchestrator.loop.TOOL_RULES", rules)
    live, live_names = _live()
    extra = _Stub(bound=True).tools()  # bound, but scope not enabled
    off = FakeLLM(["ok"])
    await _orch([*live, *extra], off).run_turn(msg)
    assert _offered(off) == live_names
    on = FakeLLM(["ok"])
    await _orch(
        [*live, *extra], on, scopes_enabled=frozenset({"tasks", "reminders", "calendar"})
    ).run_turn(msg)
    assert len(_offered(on)) == 12 and "cal_0" in _offered(on)  # cap intact


async def test_hidden_tool_call_is_still_denied_unexposed() -> None:
    rules = dict(TOOL_RULES) | {"cal_0": ToolRule(Risk.READ, "tasks", READ_PER_HOUR)}
    live, _ = _live()
    call = ProviderResponse(
        text=None,
        tool_calls=(ToolCallRequest(id="c", name="cal_0", arguments={}),),
        usage=Usage(tokens_in=1, tokens_out=1),
    )
    fake = FakeLLM([call, "done"])
    o = Orchestrator(
        build_env(provider=fake).llm,
        [*live, *_Stub(bound=False).tools()],
        PolicyEngine(PolicyConfig(rules=rules)),
        character_card="c",
    )
    r = await o.run_turn("hi")
    (ev,) = r.tool_events
    assert "cal_0" not in _offered(fake)
    assert ev.status == "denied" and ev.rule_id == "deny.unexposed"


async def test_bound_live_groups_are_offered_and_available_flag_fails_closed() -> None:
    unbound = [*TasksTool().tools(), *RemindersTool().tools()]
    assert unbound and not any(x.available() for x in unbound)  # type: ignore[attr-defined]
    fake0 = FakeLLM(["ok"])
    await _orch(unbound, fake0).run_turn("hi")
    assert _offered(fake0) == set()
    assert all(x.available() for x in _Stub(bound=True).tools())  # type: ignore[attr-defined]

    class Boom:
        spec = ToolSpec(name="boom", description="d")
        risk = Risk.READ

        def available(self) -> bool:
            raise RuntimeError

        async def run(self, args: object) -> ToolOutput:
            return ToolOutput("x")

    fake = FakeLLM(["ok"])
    await _orch([Boom()], fake).run_turn("boom")  # type: ignore[list-item]
    assert _offered(fake) == set()


def test_default_scopes_are_local_only() -> None:
    o = _orch([], FakeLLM(["ok"]))
    assert o._scopes == frozenset({"tasks", "reminders"})  # type: ignore[attr-defined]
