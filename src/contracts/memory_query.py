# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""MemoryQuery / MemoryResult: Memory agent -> Agent-runtime agent.

MemoryResult.provenance is non-null per INV-5: every derived memory row
must be traceable to its source.

P2.4 (additive; every new field has a default, so pre-P2.4 payloads parse):

* Bi-temporal reads (TARGET_DESIGN section 3 `search`). Two independent axes:
  - `as_of`    valid time: "what was true / had been said by then" (edges:
               valid_from <= as_of < valid_to; chunks: occurred_at <= as_of).
  - `known_at` record time: "what did the system know then" (rows: created_at
               <= known_at and not yet invalidated at known_at).
  Both default to None = current state.
* `include_unpromoted` (R8): off by default. Untrusted-derived rows stay
  `promoted=false` until a user approval promotes them; only review tooling
  sets this.
* `MemoryResult.lane` / `.untrusted` (INV-6): callers must render untrusted
  results as delimited DATA blocks; `untrusted` is derived from `lane` and
  cannot disagree with it.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    query_text: str
    top_k: int = Field(default=10, ge=1, le=200)
    active_entities: list[str] = Field(default_factory=list)
    present_speakers: list[UUID] = Field(default_factory=list)
    as_of: datetime | None = None
    known_at: datetime | None = None
    include_unpromoted: bool = False

    @field_validator("as_of", "known_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("as_of/known_at must be timezone-aware")
        return v


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
    lane: Literal["trusted", "untrusted"] = "trusted"
    untrusted: bool = False

    @model_validator(mode="after")
    def _untrusted_matches_lane(self) -> "MemoryResult":
        if self.untrusted != (self.lane == "untrusted"):
            raise ValueError("MemoryResult.untrusted must equal (lane == 'untrusted')")
        return self
