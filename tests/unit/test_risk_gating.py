# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""T4.3: each of the four ACT-3 risk tiers behaves correctly in the `act`
node -- READ/WRITE_REVERSIBLE auto-execute without the approver ever being
consulted; WRITE_EXTERNAL/DESTRUCTIVE go through `Approver.review` first,
and a rejection means the tool's `run()` is never called.

Uses a small in-test fake tool/registry (rather than `calendar`/`tasks`,
neither of which has a WRITE_EXTERNAL action) so all four tiers are
exercised directly, white-box, against `graph._make_act`.
"""

from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from src.contracts.tool_intent import Primitive, RiskClass, ToolIntent, ToolResult
from src.orchestrator.approver import ApprovalDecision, Approver
from src.orchestrator.graph import GraphDependencies, _make_act
from src.orchestrator.state import AgentState
from src.tools.base import BaseTool


class _FakeTool(BaseTool):
    name = "fake"
    description = "test-only tool spanning all four risk tiers"
    risk_class = RiskClass.DESTRUCTIVE
    schema: ClassVar[dict[str, dict[str, type]]] = {
        "read": {},
        "write_reversible": {},
        "write_external": {},
        "destructive": {},
    }

    def __init__(self) -> None:
        self.run_calls: list[tuple[str, dict[str, Primitive]]] = []

    def risk_class_for(self, action: str) -> RiskClass:
        return RiskClass(action)

    async def run(
        self, action: str, params: dict[str, Primitive], *, session_id: str
    ) -> ToolResult:
        self.run_calls.append((action, params))
        return ToolResult(
            tool=self.name, action=action, status="success", result={}, entity_refs={}
        )


class _DictRegistry:
    def __init__(self, tools: dict[str, BaseTool]) -> None:
        self._tools = tools

    def get(self, name: str) -> BaseTool:
        return self._tools[name]


def _state_with_intent(
    risk_class: RiskClass, params: dict[str, Primitive] | None = None
) -> AgentState:
    state = AgentState(message="do the thing")
    state.tool_calls = [
        ToolIntent(
            tool="fake",
            action=risk_class.value,
            params=params or {},
            entity_refs={},
            risk_class=risk_class,
        )
    ]
    return state


async def _run_act(tool: _FakeTool, approver: Approver, state: AgentState) -> list[ToolResult]:
    deps = GraphDependencies(tool_registry=_DictRegistry({"fake": tool}), approver=approver)
    act = _make_act(deps)
    result = await act(state, config={"configurable": {"session_id": "s1"}})
    return result["tool_results"]


@pytest.mark.asyncio
async def test_read_auto_executes_without_approver() -> None:
    tool = _FakeTool()
    approver = Approver()
    approver.review = AsyncMock(side_effect=AssertionError("approver must not be called for READ"))  # type: ignore[method-assign]
    results = await _run_act(tool, approver, _state_with_intent(RiskClass.READ))
    assert results[0].status == "success"
    assert tool.run_calls == [("read", {})]


@pytest.mark.asyncio
async def test_write_reversible_auto_executes_without_approver() -> None:
    tool = _FakeTool()
    approver = Approver()
    approver.review = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("approver must not be called for WRITE_REVERSIBLE")
    )
    results = await _run_act(tool, approver, _state_with_intent(RiskClass.WRITE_REVERSIBLE))
    assert results[0].status == "success"
    assert tool.run_calls == [("write_reversible", {})]


@pytest.mark.asyncio
async def test_write_external_goes_through_approver_and_executes_when_approved() -> None:
    tool = _FakeTool()
    approver = Approver()
    results = await _run_act(tool, approver, _state_with_intent(RiskClass.WRITE_EXTERNAL))
    assert results[0].status == "success"
    assert tool.run_calls == [("write_external", {})]


@pytest.mark.asyncio
async def test_write_external_rejected_by_approver_never_executes() -> None:
    tool = _FakeTool()
    approver = Approver()
    approver.review = lambda **_: ApprovalDecision(approved=False, reason="rejected: test")  # type: ignore[method-assign]
    results = await _run_act(tool, approver, _state_with_intent(RiskClass.WRITE_EXTERNAL))
    assert results[0].status == "error"
    assert results[0].error_message == "rejected: test"
    assert tool.run_calls == []


@pytest.mark.asyncio
async def test_destructive_without_confirmation_never_executes() -> None:
    tool = _FakeTool()
    approver = Approver()
    results = await _run_act(tool, approver, _state_with_intent(RiskClass.DESTRUCTIVE))
    assert results[0].status == "error"
    assert "confirmation" in results[0].error_message
    assert tool.run_calls == []


@pytest.mark.asyncio
async def test_destructive_with_confirmation_executes() -> None:
    tool = _FakeTool()
    approver = Approver()
    state = _state_with_intent(RiskClass.DESTRUCTIVE, params={"user_confirmed": True})
    results = await _run_act(tool, approver, state)
    assert results[0].status == "success"
    assert tool.run_calls == [("destructive", {"user_confirmed": True})]
