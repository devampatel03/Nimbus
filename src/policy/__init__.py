# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Deterministic policy (ADR-0009, INV-5): the only permission-granting code.

Pure: imports nothing from src.llm, src.net or src.db, no clock, no randomness
(pinned by `tests/eval/invariants/test_inv5_policy_engine.py`).
"""

from src.policy.carry import TaintCarry, carry_turns_for
from src.policy.config import (
    OPT_IN_CREATES,
    PENDING_IDEMPOTENCY,
    PolicyConfig,
    config_with_creates,
)
from src.policy.engine import DenyAllWritesPolicy, PolicyEngine
from src.policy.registry import LOCAL_SCOPES, TOOL_RULES, ToolRule
from src.policy.types import (
    Decision,
    Policy,
    PolicyContext,
    PolicyMode,
    ProposedCall,
    Risk,
    Verdict,
)

__all__ = [
    "LOCAL_SCOPES",
    "OPT_IN_CREATES",
    "PENDING_IDEMPOTENCY",
    "TOOL_RULES",
    "Decision",
    "DenyAllWritesPolicy",
    "Policy",
    "PolicyConfig",
    "PolicyContext",
    "PolicyEngine",
    "PolicyMode",
    "ProposedCall",
    "Risk",
    "TaintCarry",
    "ToolRule",
    "Verdict",
    "carry_turns_for",
    "config_with_creates",
]
