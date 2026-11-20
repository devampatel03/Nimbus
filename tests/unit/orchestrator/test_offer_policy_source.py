# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""#38 LOWs: what the loop offers comes from the SAME table the policy decides
with (`PolicyEngine.offerable`), not a second copy:

* a tool the policy does not know (no rule) is not offered, so it cannot take
  one of the 12 slots from a live tool;
* a tool in `disabled_tools` (e.g. `tasks_create` while PENDING_IDEMPOTENCY)
  is STILL offered and denied `deny.tool_disabled`: the protected eval test
  `test_inv5_policy_engine.py` pins that verdict (hiding it: HUMAN_TODO H28);
* a policy built with its own rules table is honoured (no skew with the
  module-level `TOOL_RULES`).

Hiding is restrictive only: a hidden tool is still denied (`deny.unexposed`).
A policy without `offerable` (e.g. `DenyAllWritesPolicy` in tests) keeps the
old scope-only filter. A failing or non-True `offerable` hides the tool."""

from __future__ import annotations

from typing import Any

import pytest

from src.llm.fake import FakeLLM
from src.llm.types import ProviderResponse, ToolCallRequest, Usage
from src.orchestrator.dispatch import Risk, ToolOutput
from src.orchestrator.loop import Orchestrator
from src.policy import (
    LOCAL_SCOPES,
    PENDING_IDEMPOTENCY,
    TOOL_RULES,
    Decision,
    PolicyConfig,
    PolicyEngine,
    ToolRule,
)
from src.policy.registry import READ_PER_HOUR
from src.tools.base import FunctionTool, object_schema
from src.tools.integrations.reminders import RemindersTool
from src.tools.integrations.tasks import TasksTool
from tests.fakes.llm import build_env


def _live() -> list[Any]:
    rem, tasks = RemindersTool(), TasksTool()
    rem._store, rem._clock = object(), object()  # type: ignore[assignment]
    tasks._sf, tasks._clock = object(), object()  # type: ignore[assignment]
    return [*rem.tools(), *tasks.tools()]


async def _run(_: Any) -> ToolOutput:
    return ToolOutput("x")


def _extra(n: int, prefix: str = "mystery") -> list[Any]:
    return [
        FunctionTool(f"{prefix}_{i}", "tasks reminders", Risk.READ, object_schema({}), _run)
        for i in range(n)
    ]


async def _offered(tools: list[Any], policy: Any, msg: str = "hi", **kw: Any) -> set[str]:
    fake = FakeLLM(["ok"])
    o = Orchestrator(build_env(provider=fake).llm, tools, policy, character_card="c", **kw)
    await o.run_turn(msg)
    return {t.name for t in fake.requests[0].tools}


# ---------------------------------------------------------------- PolicyEngine.offerable


def test_offerable_follows_the_policy_table() -> None:
    p = PolicyEngine()
    assert p.offerable("tasks_list", LOCAL_SCOPES) is True
    assert p.offerable("calendar_list", LOCAL_SCOPES) is False  # scope off
    assert p.offerable("calendar_list", LOCAL_SCOPES | {"calendar"}) is True
    assert p.offerable("mystery_0", LOCAL_SCOPES) is False  # no rule
    for name in PENDING_IDEMPOTENCY:
        assert p.offerable(name, LOCAL_SCOPES) is True  # disabled, but pinned as offered


@pytest.mark.parametrize("scopes", [{"tasks"}, ["tasks"], None, "tasks"])
def test_offerable_needs_a_frozenset(scopes: Any) -> None:
    assert PolicyEngine().offerable("tasks_list", scopes) is False


@pytest.mark.parametrize("name", [None, 3, b"tasks_list"])
def test_offerable_refuses_non_str_names(name: Any) -> None:
    assert PolicyEngine().offerable(name, LOCAL_SCOPES) is False


def test_offerable_with_a_broken_rules_table_is_false() -> None:
    class Boom(dict):  # type: ignore[type-arg]
        def get(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError

    assert PolicyEngine(PolicyConfig(rules=Boom())).offerable("tasks_list", LOCAL_SCOPES) is False


# ---------------------------------------------------------------- the loop


async def test_all_known_live_tools_are_offered_under_the_engine() -> None:
    live = _live()
    got = await _offered(live, PolicyEngine(), "remind me to call mom at 5")
    assert got == {t.spec.name for t in live}


async def test_unknown_rule_tools_take_no_cap_slots() -> None:
    live = _live()
    want = {t.spec.name for t in live}
    got = await _offered([*_extra(12), *live], PolicyEngine(), "tasks reminders")
    assert got == want


async def test_policy_rules_are_the_source_not_the_module_table() -> None:
    rules = dict(TOOL_RULES) | {
        "mystery_0": ToolRule(Risk.READ, "tasks", READ_PER_HOUR),
        # skew: the module table says tasks_list is scope "tasks"; this policy says "calendar"
        "tasks_list": ToolRule(Risk.READ, "calendar", READ_PER_HOUR),
    }
    policy = PolicyEngine(PolicyConfig(rules=rules, disabled_tools=frozenset()))
    got = await _offered([*_extra(1), *_live()], policy, "tasks")
    assert "mystery_0" in got and "tasks_list" not in got


async def test_offerable_failure_hides_and_hidden_call_is_denied_unexposed() -> None:
    class Flaky(PolicyEngine):
        def offerable(self, name: str, scopes: frozenset[str]) -> bool:
            if name == "tasks_list":
                raise RuntimeError("boom")
            if name == "tasks_get":
                return 1  # type: ignore[return-value] - truthy but not True
            return super().offerable(name, scopes)

    call = ProviderResponse(
        text=None,
        tool_calls=(ToolCallRequest(id="c", name="tasks_list", arguments={}),),
        usage=Usage(tokens_in=1, tokens_out=1),
    )
    fake = FakeLLM([call, "done"])
    o = Orchestrator(build_env(provider=fake).llm, _live(), Flaky(), character_card="c")
    r = await o.run_turn("tasks")
    offered = {t.name for t in fake.requests[0].tools}
    assert "tasks_list" not in offered and "tasks_get" not in offered
    (ev,) = r.tool_events
    assert ev.status == "denied" and ev.rule_id == "deny.unexposed"


async def test_policy_without_offerable_keeps_the_scope_filter() -> None:
    class Plain:
        def decide(self, call: Any, ctx: Any) -> Decision:
            return PolicyEngine().decide(call, ctx)

    got = await _offered([*_extra(2), *_live()], Plain(), "tasks")
    assert {"mystery_0", "mystery_1"} <= got  # legacy: unknown-rule tools pass the filter
