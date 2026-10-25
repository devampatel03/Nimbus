# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""T1.10: a multi-turn conversation produces coherent responses.

this session update: `think`/`respond` now call a real `LLMClient` by default
(`GraphDependencies.llm_client` defaults to `VLLMClient()`), so every test
here injects `_FakeLLM` -- a small, deterministic fake reproducing the
kind of tool-triggering/response behavior a real local model would
produce for these fixed test messages, keeping these tests fast and
hermetic (no real vLLM server needed) rather than asserting against the
exact wording a real model would generate.

Scope note: T1.10's full acceptance criterion (a 20-turn labelled eval
set, P-EVAL-graded) still needs a real generation model actually running
to grade; what is tested here is structural coherence over multiple
turns against a fake standing in for that model: conversation history
accumulates correctly and in order, roles alternate, every turn produces
a non-empty response, and a tool-triggering turn surfaces its result in
that same turn's response without corrupting later turns.
"""

import pytest

from src.llm.client import ChatMessage
from src.orchestrator.graph import GraphDependencies, build_graph
from src.orchestrator.state import AgentState

_CALENDAR_TRIGGERS = ("schedule", "meeting", "calendar event")
_TASK_TRIGGERS = ("remind me", "add a task", "todo", "to-do")


class _FakeLLM:
    """Deterministic stand-in for a real local model: `structured` mirrors
    the earlier sessions' keyword heuristic (a reasonable fake for what a
    real model would decide on these fixed test messages); `complete`
    echoes any tool-results system note it's given so tests can assert on
    it without depending on a specific generated wording.
    """

    async def structured(
        self, messages: list[ChatMessage], *, schema: type, temperature: float = 0.0
    ):
        user_text = messages[-1]["content"].lower()
        title = messages[-1]["content"].strip()[:80] or "untitled"
        if any(trigger in user_text for trigger in _CALENDAR_TRIGGERS):
            return schema(
                tool="calendar", action="create", params={"title": title, "start": "", "end": ""}
            )
        if any(trigger in user_text for trigger in _TASK_TRIGGERS):
            return schema(tool="tasks", action="create", params={"title": title})
        return schema(tool=None, action=None, params={})

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        tool_notes = [
            m["content"]
            for m in messages
            if m["role"] == "system" and m["content"].startswith("Tool execution results")
        ]
        if tool_notes:
            return f"Done. {tool_notes[0]}"
        return "Got it."

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ):
        raise NotImplementedError


def _deps() -> GraphDependencies:
    return GraphDependencies(llm_client=_FakeLLM())


@pytest.mark.asyncio
async def test_multi_turn_conversation_accumulates_coherent_history() -> None:
    graph = build_graph(_deps())
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
    graph = build_graph(_deps())
    config = {"configurable": {"session_id": "multi-turn-tool-session"}}

    history: list = []

    state = AgentState(message="remind me to call the bank", conversation_history=history)
    result = await graph.ainvoke(state, config=config)
    assert "tasks.create" in result["response"]
    history = result["conversation_history"]

    state2 = AgentState(message="thanks", conversation_history=history)
    result2 = await graph.ainvoke(state2, config=config)
    assert result2["response"]
    assert result2["tool_calls"] == []
    assert len(result2["conversation_history"]) == len(history) + 2


@pytest.mark.asyncio
async def test_removed_calendar_tool_is_never_planned_or_executed() -> None:
    """P1.4c (M9): the scrubber-backed calendar intent builder is deleted. A
    model that still proposes `calendar.create` has the proposal dropped at
    `think` (unknown tool), so no tool call or tool result exists."""
    graph = build_graph(_deps())
    config = {"configurable": {"session_id": "calendar-session"}}

    state = AgentState(message="schedule a meeting tomorrow", conversation_history=[])
    result = await graph.ainvoke(state, config=config)

    assert result["tool_calls"] == []
    assert result["tool_results"] == []
    assert "calendar" not in _deps().tool_registry.names()
    assert result["response"]
