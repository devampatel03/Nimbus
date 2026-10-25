# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""ToolIntent / ToolResult: orchestrator -> tool. Frozen at P3; deleted in P2.5b.

Originally the ONLY object the (now deleted, P1.4c) scrubber's
`execute_external` accepted. Per section 8.1: no transcripts, memory rows, graph edges,
personality traits, stance entries, reasoning chains, raw user utterances
or person names may cross this boundary — params are primitives only and
entities are opaque local refs.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict

Primitive = str | int | float | bool | None


class RiskClass(str, Enum):
    """Section 9.1 risk model."""

    READ = "read"
    WRITE_REVERSIBLE = "write_reversible"
    WRITE_EXTERNAL = "write_external"
    DESTRUCTIVE = "destructive"


class ToolIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str  # from the registry, validated
    action: str  # from the tool's declared actions
    params: dict[str, Primitive]  # primitives only, no nested objects
    entity_refs: dict[str, str]  # opaque local refs: "<person_44>"
    risk_class: RiskClass


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    action: str
    status: str  # success | error
    result: dict[str, Primitive]
    entity_refs: dict[str, str]
    error_message: str | None = None
