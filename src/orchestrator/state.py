# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""AgentState: the LangGraph state schema for `src/orchestrator/graph.py`
(docs/TECHNICAL_ARCHITECTURE.md sections 4, 9).

This is an internal orchestrator-runtime object, not one of the six frozen
`src/contracts/` interfaces -- it is mutated node-to-node within a single
turn of the graph and never crosses the scrubber boundary itself (nodes
build a `ToolIntent` from slices of it instead; see `act` in `graph.py`).

Field types deliberately reuse the frozen contracts where a field is
sourced from another pod's output (`MemoryResult`, `UserProfileSlice`,
`ToolIntent`/`ToolResult`) rather than redeclaring shapes those pods own.
"""

from dataclasses import dataclass, field

from src.contracts.memory_query import MemoryResult
from src.contracts.tool_intent import RiskClass, ToolIntent, ToolResult
from src.contracts.user_profile_slice import UserProfileSlice


@dataclass
class ConversationTurn:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class AgentState:
    message: str
    conversation_history: list[ConversationTurn] = field(default_factory=list)
    retrieved_memories: list[MemoryResult] = field(default_factory=list)
    personality_slice: UserProfileSlice | None = None
    tool_calls: list[ToolIntent] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    response: str = ""
    risk_class: RiskClass | None = None
