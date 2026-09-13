# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""T1.10: a multi-turn conversation produces coherent responses.

Scope note: T1.10's full acceptance criterion (a 20-turn labelled eval
set, P-EVAL-graded) needs a real generation model wired into `respond`
(see `src/orchestrator/graph.py`'s module docstring -- no local model is
integrated at the orchestrator layer yet). What is tested here is
structural coherence over multiple turns: conversation history accumulates
correctly and in order, roles alternate, every turn produces a non-empty
response, and a tool-triggering turn surfaces its result in that same
turn's response without corrupting later turns.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.contracts.tool_intent import ToolResult
from src.orchestrator.graph import build_graph
from src.orchestrator.state import AgentState


@pytest.mark.asyncio
async def test_multi_turn_conversation_accumulates_coherent_history() -> None:
    graph = build_graph()
    config = {"configurable": {"session_id": "multi-turn-session"}}

    history: list = []
    messages = [
        "hello",
        "what do you know about my day",
        "thanks, that's helpful",
    ]

    for message in messages:
        state = AgentState(message=message, conversation_history=history)
        result = await graph.ainvoke(state, config=config)
        assert result["response"]
        history = result["conversation_history"]

    assert len(history) == 2 * len(messages)
    roles = [turn.role for turn in history]
    assert roles == ["user", "assistant"] * len(messages)
    for turn, expected_message in zip(history[0::2], messages, strict=True):
        assert turn.content == expected_message


@pytest.mark.asyncio
async def test_tool_triggering_turn_surfaces_result_without_breaking_next_turn() -> None:
    graph = build_graph()
    config = {"configurable": {"session_id": "multi-turn-tool-session"}}

    history: list = []

    state = AgentState(message="remind me to call the bank", conversation_history=history)
    result = await graph.ainvoke(state, config=config)
    assert "Done: tasks.create" in result["response"]
    history = result["conversation_history"]

    state2 = AgentState(message="thanks", conversation_history=history)
    result2 = await graph.ainvoke(state2, config=config)
    assert result2["response"]
    assert result2["tool_calls"] == []
    assert len(result2["conversation_history"]) == len(history) + 2


@pytest.mark.asyncio
async def test_calendar_tool_call_routes_through_scrubber_not_a_second_egress_point() -> None:
    graph = build_graph()
    config = {"configurable": {"session_id": "calendar-session"}}

    stub_result = ToolResult(
        tool="calendar",
        action="create",
        status="success",
        result={"event_id": "evt-1"},
        entity_refs={},
    )
    with patch(
        "src.tools.integrations.calendar.execute_external", new=AsyncMock(return_value=stub_result)
    ) as mock_execute:
        state = AgentState(message="schedule a meeting tomorrow", conversation_history=[])
        result = await graph.ainvoke(state, config=config)

    mock_execute.assert_awaited_once()
    assert result["tool_results"][0].status == "success"
    assert "Done: calendar.create" in result["response"]
