# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Tasks and reminders as `LoopTool`s (Tool protocol v2, P2.5b): one tool per
action, risk declared by code, output untrusted by default, all inputs
validated against the declared JSON schema before anything runs."""

from __future__ import annotations

from typing import Any

import pytest

from src.orchestrator.dispatch import DenyAllWritesPolicy, Dispatcher, Risk, ToolError
from src.tools.base import ToolGroup
from src.tools.integrations.reminders import RemindersTool
from src.tools.integrations.tasks import TasksTool
from src.tools.registry import ToolRegistry


def _by_name(group: ToolGroup) -> dict[str, Any]:
    return {t.spec.name: t for t in group.tools()}


EXPECTED_RISKS: dict[str, Risk] = {
    "tasks_create": Risk.WRITE_INTERNAL,
    "tasks_update": Risk.WRITE_INTERNAL,
    "tasks_complete": Risk.WRITE_INTERNAL,
    "tasks_delete": Risk.WRITE_INTERNAL,
    "tasks_get": Risk.READ,
    "tasks_list": Risk.READ,
    "reminders_create": Risk.WRITE_INTERNAL,
    "reminders_update": Risk.WRITE_INTERNAL,
    "reminders_delete": Risk.WRITE_INTERNAL,
    "reminders_get": Risk.READ,
    "reminders_list": Risk.READ,
}


def test_risk_is_declared_per_tool_by_code() -> None:
    """Exact map: an added, removed or mislabelled tool fails this test."""
    actual = {t.spec.name: t.risk for t in ToolRegistry().loop_tools()}
    assert actual == EXPECTED_RISKS


async def test_unbound_groups_fail_closed_with_no_database() -> None:
    """The registry builds groups with no arguments; until the app binds them
    to Postgres every tool refuses (P3.3). Nothing is held in process memory."""
    for group in (TasksTool(), RemindersTool()):
        for tool in group.tools():
            args = {k: "x" for k in tool.spec.parameters["properties"]}
            with pytest.raises(ToolError, match="unavailable"):
                await tool.run(args)


@pytest.mark.parametrize(
    "args",
    [{}, {"title": 5}, {"title": "x", "extra": 1}, {"title": None}],
)
async def test_invalid_args_rejected_before_running(args: dict[str, Any]) -> None:
    with pytest.raises(ToolError, match="invalid arguments"):
        await _by_name(TasksTool())["tasks_create"].run(args)


async def test_deny_all_writes_policy_gates_real_tools() -> None:
    registry = ToolRegistry()
    tools = {t.spec.name: t for t in registry.loop_tools()}
    d = Dispatcher(tools, DenyAllWritesPolicy())
    exposed = frozenset(tools)
    denied = await d.dispatch("tasks_create", {"title": "x"}, exposed=exposed)
    assert denied.status == "denied"
    assert denied.rule_id == "deny.write_interim"
    # A READ is allowed by policy; the unbound tool then fails closed (no DB here).
    allowed = await d.dispatch("tasks_list", {}, exposed=exposed)
    assert allowed.status == "error"
