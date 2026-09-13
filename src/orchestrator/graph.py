# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The agent runtime graph (docs/TECHNICAL_ARCHITECTURE.md sections 4, 9;
T1.10, T4.1-T4.5): route -> retrieve_memory -> think -> act -> respond.

Design decisions worth flagging (see MEMORY.md AD-14):

- `retrieve_memory` reads `MemoryManager.get_warm_context` (Redis-only,
  ~1ms per T1.7's measurement), not the full Qdrant+Postgres `query()` --
  this is the Mode A-safe read; the expensive retrieval already happens
  off-path in `refresh_warm_context` per T1.7's own design.
- `think` has no local model wired in yet (same constraint as
  `check_friction`/AD-11, the scrubber's PII detector/AD-13, and the
  stub embedder/AD-7): it is a deterministic keyword heuristic proposing
  at most one `ToolIntent`, not a real reasoning step. This is a
  structural placeholder to exercise `act`'s risk-gating and the
  approver, not T1.10's eventual acceptance bar (20-turn coherence eval,
  owned by P-EVAL, needs a real generation model).
- `act` calls `tool.run(...)` for every proposed intent after the
  approval gate. INV-1 compliance is transitive: `CalendarTool.run`
  (the only tool here needing a third-party service) calls
  `scrubber.execute_external` internally; `TasksTool.run` is genuinely
  local and never reaches the scrubber (see `src/tools/base.py`'s
  docstring). `act` itself never imports `httpx`/holds a second egress
  call site.
- `respond` calls `context_builder.build_system_prompt`, which only does
  a local (non-network) file read plus string formatting -- no network
  call sits on this path, satisfying INV-4. Response text itself is
  template-composed, not model-generated (no local generation model is
  wired into the orchestrator yet -- vLLM serving, T1.5, is not
  integrated at the orchestrator layer in this session's scope).
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.contracts.tool_intent import RiskClass, ToolIntent, ToolResult
from src.memory.manager import MemoryManager
from src.orchestrator import context_builder
from src.orchestrator.approver import Approver
from src.orchestrator.state import AgentState, ConversationTurn
from src.tools.registry import ToolRegistry

# `langchain_core` is not in pyproject.toml's own `dependencies` list, but
# it is `langgraph`'s own required dependency (its node-signature protocol,
# `_NodeWithConfig`, is typed against exactly this `RunnableConfig`), so
# importing it here does not introduce a separately-versioned dependency.
Config = RunnableConfig

_RISK_ORDER: dict[RiskClass, int] = {
    RiskClass.READ: 0,
    RiskClass.WRITE_REVERSIBLE: 1,
    RiskClass.WRITE_EXTERNAL: 2,
    RiskClass.DESTRUCTIVE: 3,
}
_GATED_RISK_CLASSES = frozenset({RiskClass.WRITE_EXTERNAL, RiskClass.DESTRUCTIVE})

_MAX_TITLE_LEN = 80


@dataclass
class GraphDependencies:
    memory_manager: MemoryManager | None = None
    tool_registry: ToolRegistry = field(default_factory=ToolRegistry)
    approver: Approver = field(default_factory=Approver)


def _get_session_id(config: Config | None) -> str:
    if not config:
        return "default-session"
    session_id = config.get("configurable", {}).get("session_id", "default-session")
    return str(session_id)


# -- route ----------------------------------------------------------------


def _route(state: AgentState) -> dict[str, Any]:
    history = [*state.conversation_history, ConversationTurn(role="user", content=state.message)]
    return {"conversation_history": history}


# -- retrieve_memory --------------------------------------------------------


def _make_retrieve_memory(
    deps: GraphDependencies,
) -> Callable[[AgentState, Config], Awaitable[dict[str, Any]]]:
    async def retrieve_memory(state: AgentState, config: Config) -> dict[str, Any]:
        if deps.memory_manager is None:
            return {"retrieved_memories": []}
        session_id = _get_session_id(config)
        warm = await deps.memory_manager.get_warm_context(uuid.UUID(session_id))
        if warm is None:
            return {"retrieved_memories": []}
        return {"retrieved_memories": warm.results}

    return retrieve_memory


# -- think ------------------------------------------------------------------

_CALENDAR_TRIGGERS = ("schedule", "meeting", "calendar event")
_TASK_TRIGGERS = ("remind me", "add a task", "todo", "to-do")


def _propose_tool_calls(message: str) -> list[ToolIntent]:
    """Heuristic stand-in for local-model tool planning. See module
    docstring -- no local model is wired in yet.
    """
    lowered = message.lower()
    title = message.strip()[:_MAX_TITLE_LEN] or "untitled"

    if any(trigger in lowered for trigger in _CALENDAR_TRIGGERS):
        return [
            ToolIntent(
                tool="calendar",
                action="create",
                params={"title": title, "start": "", "end": ""},
                entity_refs={},
                risk_class=RiskClass.WRITE_REVERSIBLE,
            )
        ]
    if any(trigger in lowered for trigger in _TASK_TRIGGERS):
        return [
            ToolIntent(
                tool="tasks",
                action="create",
                params={"title": title},
                entity_refs={},
                risk_class=RiskClass.WRITE_REVERSIBLE,
            )
        ]
    return []


def _think(state: AgentState) -> dict[str, Any]:
    tool_calls = _propose_tool_calls(state.message)
    risk_class = None
    if tool_calls:
        risk_class = max((tc.risk_class for tc in tool_calls), key=_RISK_ORDER.__getitem__)
    return {"tool_calls": tool_calls, "risk_class": risk_class}


def _think_router(state: AgentState) -> str:
    return "act" if state.tool_calls else "respond"


# -- act ----------------------------------------------------------------


def _make_act(deps: GraphDependencies) -> Callable[[AgentState, Config], Awaitable[dict[str, Any]]]:
    async def act(state: AgentState, config: Config) -> dict[str, Any]:
        session_id = _get_session_id(config)
        results: list[ToolResult] = []
        for intent in state.tool_calls:
            try:
                tool = deps.tool_registry.get(intent.tool)
            except KeyError as exc:
                results.append(
                    ToolResult(
                        tool=intent.tool,
                        action=intent.action,
                        status="error",
                        result={},
                        entity_refs=intent.entity_refs,
                        error_message=str(exc),
                    )
                )
                continue

            risk = tool.risk_class_for(intent.action)
            if risk in _GATED_RISK_CLASSES:
                decision = deps.approver.review(
                    proposed_action=intent.params,
                    original_intent=state.message,
                    risk_class=risk,
                )
                if not decision.approved:
                    results.append(
                        ToolResult(
                            tool=intent.tool,
                            action=intent.action,
                            status="error",
                            result={},
                            entity_refs=intent.entity_refs,
                            error_message=decision.reason,
                        )
                    )
                    continue

            result = await tool.run(intent.action, intent.params, session_id=session_id)
            results.append(result)

        return {"tool_results": results}

    return act


# -- respond ------------------------------------------------------------


def _compose_response(state: AgentState) -> str:
    if state.tool_results:
        parts = []
        for result in state.tool_results:
            if result.status == "success":
                parts.append(f"Done: {result.tool}.{result.action}.")
            else:
                parts.append(f"Couldn't {result.tool}.{result.action}: {result.error_message}")
        return " ".join(parts)

    if state.retrieved_memories:
        return f"({len(state.retrieved_memories)} relevant memory item(s) considered) Got it."
    return "Got it."


def _respond(state: AgentState) -> dict[str, Any]:
    context_builder.build_system_prompt(state)  # local file read only -- INV-4 safe
    response_text = _compose_response(state)
    history = [
        *state.conversation_history,
        ConversationTurn(role="assistant", content=response_text),
    ]
    return {"response": response_text, "conversation_history": history}


# -- graph assembly -------------------------------------------------------


def build_graph(
    deps: GraphDependencies | None = None,
) -> CompiledStateGraph[AgentState, Any, Any, Any]:
    deps = deps or GraphDependencies()
    graph: StateGraph[AgentState, Any, Any, Any] = StateGraph(AgentState)

    graph.add_node("route", _route)
    # mypy can't unify `add_node`'s overloaded node-Protocol match for an
    # async (state, config) callable against this StateGraph's inferred
    # generics in this langgraph version -- the sync, single-arg nodes
    # above/below type-check fine, isolating this to that overload pair.
    graph.add_node("retrieve_memory", _make_retrieve_memory(deps))  # type: ignore[arg-type]
    graph.add_node("think", _think)
    graph.add_node("act", _make_act(deps))  # type: ignore[arg-type]
    graph.add_node("respond", _respond)

    graph.set_entry_point("route")
    graph.add_edge("route", "retrieve_memory")
    graph.add_edge("retrieve_memory", "think")
    graph.add_conditional_edges("think", _think_router, {"act": "act", "respond": "respond"})
    graph.add_edge("act", "respond")
    graph.add_edge("respond", END)

    return graph.compile()
