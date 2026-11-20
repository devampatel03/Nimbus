# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""ProactiveCandidate: All scanners -> Scheduler. Frozen at P4.

Section 9.3: scanners produce candidates, each scored on expected value;
a hard daily budget (6/day default) admits only the top candidates.
Losers stay queued and decay.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ProactiveCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: UUID
    scanner: str
    user_id: UUID
    summary: str
    expected_value: float
    created_at: datetime
    decay_at: datetime | None = None
    status: str = "queued"  # queued | admitted | expired | dismissed
