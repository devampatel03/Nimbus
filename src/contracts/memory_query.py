# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""MemoryQuery / MemoryResult: Memory agent -> Agent-runtime agent. Frozen at P2.

MemoryResult.provenance is non-null per INV-5: every derived memory row
must be traceable to its source.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    query_text: str
    top_k: int = 10
    active_entities: list[str] = Field(default_factory=list)
    present_speakers: list[UUID] = Field(default_factory=list)


class MemoryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    memory_id: UUID
    content: str
    score: float
    source_session: UUID
    provenance: str  # INV-5: never null
    valid_from: datetime
    valid_to: datetime | None = None
    confidence: float
