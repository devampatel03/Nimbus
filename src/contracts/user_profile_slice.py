# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""UserProfileSlice: Personality agent -> Agent-runtime agent. Frozen at P3.

The warm context slice described in section 4: precomputed so a Mode A
turn can read it from Redis instead of querying traits/stances live
(INV-4 — the response path never blocks on a network call).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TraitSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str  # communication | decision | energy | value | pattern | preference
    statement: str
    confidence: float  # PSU-2: only traits >= 0.6 belong in a slice


class StanceSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    topic: str
    position: str
    strength: str  # tentative | held | firm


class UserProfileSlice(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    traits: list[TraitSummary] = Field(default_factory=list)
    active_stances: list[StanceSummary] = Field(default_factory=list)
    generated_at: datetime
    version: int
