# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Frozen interface contracts between agent pods (TECHNICAL_ARCHITECTURE.md section 12).

Each contract here is versioned and fixture-tested. Do not remove a field or
change its type without a documented amendment — see docs/TECHNICAL_ARCHITECTURE.md
section 12 and tests/unit/test_contracts.py.
"""

from src.contracts.diarized_transcript import DiarizedTranscript, Utterance
from src.contracts.memory_query import MemoryQuery, MemoryResult
from src.contracts.proactive_candidate import ProactiveCandidate
from src.contracts.session_bundle import SessionBundle
from src.contracts.tool_intent import Primitive, RiskClass, ToolIntent, ToolResult
from src.contracts.user_profile_slice import (
    StanceSummary,
    TraitSummary,
    UserProfileSlice,
)

__all__ = [
    "DiarizedTranscript",
    "MemoryQuery",
    "MemoryResult",
    "Primitive",
    "ProactiveCandidate",
    "RiskClass",
    "SessionBundle",
    "StanceSummary",
    "ToolIntent",
    "ToolResult",
    "TraitSummary",
    "UserProfileSlice",
    "Utterance",
]
