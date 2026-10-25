# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The agent runtime graph (docs/TECHNICAL_ARCHITECTURE.md sections 4, 9;
T1.10, T4.1-T4.5, this session): route -> retrieve_memory -> think -> act -> respond.

Design decisions worth flagging (see MEMORY.md AD-14 and this session's AD):

- `retrieve_memory` is a no-op placeholder (P1.3a): the Redis warm-context
  cache was deleted in the pivot (TARGET_DESIGN M5); retrieval becomes a
  direct Postgres/pgvector call in a later task.
- `think`/`respond` (this session) call `deps.llm_client` (a local vLLM-backed
  `LLMClient`, `src/llm/client.py`) for real tool-planning and response
  generation, replacing the earlier sessions' keyword heuristic and
  template. This is NOT cloud egress -- INV-1 is unaffected (the local
  client never imports httpx/requests/aiohttp directly, and is never
  routed through the scrubber). `GraphDependencies.llm_client` defaults
  to a real `VLLMClient()`, same default-factory pattern as
  `MemoryManager`'s embedder (AD-7/Step 11) -- production gets it for
  free, tests inject a fake explicitly (`tests/unit/test_graph.py`).
- `act` calls `tool.run(...)` for every proposed intent after the
  approval gate, now via `deps.approver.review_async` (this session's
  LLM-based judgment; see `approver.py`'s module docstring for why the
  sync `review` heuristic is kept alongside it, unchanged, for the
  protected `tests/eval/injection/red_team.py` harness). The registered
  tools (tasks, reminders) are purely local; the scrubber-backed
  calendar/gmail/drive intent builders were deleted in P1.4c. `act`
  itself imports no transport (INV-1: only `src/net` may).
- `respond` calls `context_builder.build_system_prompt`, which only does
  a local (non-network) file read plus string formatting before handing
  the assembled prompt to the LLM client -- the local model call is the
  only network-shaped step in this node, and per the module note above
  it is a loopback call, not cloud egress (INV-1 unaffected). Whether a
  local-model call belongs on the Mode A hot path per INV-4's literal
  wording is a question for `src/orchestrator/mode_a.py`, which does not
  exist yet (INV-4's own test is still a stub) -- flagged here rather
  than silently assumed either way.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field

from src.contracts.tool_intent import Primitive, RiskClass, ToolIntent, ToolResult
from src.llm.client import ChatMessage, LLMClient, VLLMClient
from src.memory.manager import MemoryManager
from src.orchestrator import context_builder
from src.orchestrator.approver import Approver
from src.orchestrator.state import AgentState, ConversationTurn
from src.tools.base import BaseTool
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


@dataclass
class GraphDependencies:
    memory_manager: MemoryManager | None = None
    tool_registry: ToolRegistry = field(default_factory=ToolRegistry)
    llm_client: LLMClient = field(default_factory=VLLMClient)
    approver: Approver | None = None

    def __post_init__(self) -> None:
        # Share one LLM client between think/respond and the approver
        # rather than each constructing its own, unless the caller
        # explicitly injected a distinct `Approver`.
        if self.approver is None:
            self.approver = Approver(llm_client=self.llm_client)


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
        return {"retrieved_memories": []}

    return retrieve_memory


# -- think ------------------------------------------------------------------

_THINK_SYSTEM_PROMPT = (
    "You are a tool-planning module for a personal assistant agent. Given "
    "the user's message, decide whether a tool call is needed right now. "
    "Respond only via the given JSON schema. If no tool call is needed, set "
    "tool and action to null and params to an empty object.\n\n"
    "Available tools:\n{catalog}"
)


class _ToolProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    action: str | None = None
    params: dict[str, Primitive] = Field(default_factory=dict)


def _tool_catalog_text(registry: ToolRegistry) -> str:
    lines = []
    for name in registry.names():
        tool = registry.get(name)
        actions = ", ".join(tool.schema)
        lines.append(f"- {tool.name}: {tool.description} (actions: {actions})")
    return "\n".join(lines) or "(no tools available)"


def _resolve_proposed_tool(
    registry: ToolRegistry, proposal: _ToolProposal
) -> tuple[BaseTool, str] | None:
    if not proposal.tool or not proposal.action:
        return None
    try:
        tool = registry.get(proposal.tool)
        tool.validate_params(proposal.action, proposal.params)
    except (KeyError, ValueError):
        return None
    return tool, proposal.action


def _make_think(deps: GraphDependencies) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def think(state: AgentState) -> dict[str, Any]:
        catalog = _tool_catalog_text(deps.tool_registry)
        messages: list[ChatMessage] = [
            {"role": "system", "content": _THINK_SYSTEM_PROMPT.format(catalog=catalog)},
            {"role": "user", "content": state.message},
        ]
        proposal = await deps.llm_client.structured(messages, schema=_ToolProposal)

        tool_calls: list[ToolIntent] = []
        resolved = _resolve_proposed_tool(deps.tool_registry, proposal)
        if resolved is not None:
            tool, action = resolved
            assert proposal.tool is not None and proposal.action is not None
            tool_calls.append(
                ToolIntent(
                    tool=proposal.tool,
                    action=proposal.action,
                    params=proposal.params,
                    entity_refs={},
                    risk_class=tool.risk_class_for(action),
                )
            )

        risk_class = None
        if tool_calls:
            risk_class = max((tc.risk_class for tc in tool_calls), key=_RISK_ORDER.__getitem__)
        return {"tool_calls": tool_calls, "risk_class": risk_class}

    return think


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
                assert deps.approver is not None  # set in __post_init__
                decision = await deps.approver.review_async(
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


def _tool_results_summary(results: list[ToolResult]) -> str | None:
    if not results:
        return None
    parts = []
    for result in results:
        if result.status == "success":
            parts.append(f"{result.tool}.{result.action} succeeded.")
        else:
            parts.append(f"{result.tool}.{result.action} failed: {result.error_message}")
    return "Tool execution results (mention these naturally, do not invent others): " + " ".join(
        parts
    )


def _make_respond(deps: GraphDependencies) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def respond(state: AgentState) -> dict[str, Any]:
        system_prompt = context_builder.build_system_prompt(state)  # local file read only
        messages: list[ChatMessage] = [{"role": "system", "content": system_prompt}]
        messages.extend(
            {"role": turn.role, "content": turn.content} for turn in state.conversation_history
        )
        tool_summary = _tool_results_summary(state.tool_results)
        if tool_summary:
            messages.append({"role": "system", "content": tool_summary})

        response_text = await deps.llm_client.complete(messages)
        history = [
            *state.conversation_history,
            ConversationTurn(role="assistant", content=response_text),
        ]
        return {"response": response_text, "conversation_history": history}

    return respond


# -- graph assembly -------------------------------------------------------


def build_graph(
    deps: GraphDependencies | None = None,
) -> CompiledStateGraph[AgentState, Any, Any, Any]:
    deps = deps or GraphDependencies()
    graph: StateGraph[AgentState, Any, Any, Any] = StateGraph(AgentState)

    graph.add_node("route", _route)
    # mypy can't unify `add_node`'s overloaded node-Protocol match for an
    # async callable against this StateGraph's inferred generics in this
    # langgraph version -- the sync nodes type-check fine, isolating this
    # to the async overload pairs (both the (state, config) and the
    # single-arg async shapes hit the same mypy limitation).
    graph.add_node("retrieve_memory", _make_retrieve_memory(deps))  # type: ignore[arg-type]
    graph.add_node("think", _make_think(deps))  # type: ignore[arg-type]
    graph.add_node("act", _make_act(deps))  # type: ignore[arg-type]
    graph.add_node("respond", _make_respond(deps))  # type: ignore[arg-type]

    graph.set_entry_point("route")
    graph.add_edge("route", "retrieve_memory")
    graph.add_edge("retrieve_memory", "think")
    graph.add_conditional_edges("think", _think_router, {"act": "act", "respond": "respond"})
    graph.add_edge("act", "respond")
    graph.add_edge("respond", END)

    return graph.compile()
