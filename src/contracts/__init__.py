# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Frozen interface contracts (TECHNICAL_ARCHITECTURE.md section 12; Event/Transcript from P2.2).

Each contract here is versioned and fixture-tested. Do not remove a field or
change its type without a documented amendment — see docs/TECHNICAL_ARCHITECTURE.md
section 12 and tests/unit/test_contracts.py.
"""

from src.contracts.event import Event, EventKind
from src.contracts.memory_query import MemoryQuery, MemoryResult
from src.contracts.proactive_candidate import ProactiveCandidate
from src.contracts.transcript import Segment, Transcript
from src.contracts.user_profile_slice import (
    StanceSummary,
    TraitSummary,
    UserProfileSlice,
)

__all__ = [
    "Event",
    "EventKind",
    "MemoryQuery",
    "MemoryResult",
    "ProactiveCandidate",
    "Segment",
    "StanceSummary",
    "TraitSummary",
    "Transcript",
    "UserProfileSlice",
]
