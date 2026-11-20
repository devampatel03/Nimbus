# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Policy value types (ADR-0009, INV-5).

`ProposedCall` carries what the model asked for (tool name, arguments) plus the
risk class that CODE declared on the tool. `PolicyContext` carries only facts
computed by code (taint, mode, counters, scopes, approval state). `Decision` is
the only grant-shaped value in `src/`; it is constructed only by the helpers at
the bottom of this module, which only `src.policy` calls (pinned by
`tests/eval/invariants/test_inv5_policy_engine.py`).

This module is pure: no I/O, no clock, no randomness, no LLM, network or DB.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

Verdict = Literal["allow", "require_approval", "deny"]
VERDICTS: frozenset[str] = frozenset({"allow", "require_approval", "deny"})


class Risk(StrEnum):
    READ = "read"
    WRITE_INTERNAL = "write_internal"
    WRITE_EXTERNAL = "write_external"
    DESTRUCTIVE = "destructive"


class PolicyMode(StrEnum):
    """Mirror of `src.llm.types.LLMMode` (the policy must not import src.llm)."""

    NORMAL = "normal"
    READ_ONLY = "read_only"  # daily spend cap hit (R5)
    KILLED = "killed"  # kill switch engaged


@dataclass(frozen=True)
class ProposedCall:
    tool: str  # model-chosen name
    args: Mapping[str, Any]  # model-chosen; the policy never reads values
    risk: Risk | None  # declared by the tool's code; None = unknown tool


@dataclass(frozen=True, kw_only=True)
class PolicyContext:
    """Code-derived facts only. Every field is set by the dispatcher (or, for
    the two approval fields, by P3.2's consumed approval), never by the model.
    Non-bool values fail closed in the engine (see `engine._facts`)."""

    exposed: bool  # the tool was offered to the model this turn
    tainted: bool  # untrusted-derived data entered this turn or a carried one
    mode: PolicyMode  # kill switch / spend cap state
    scopes_enabled: frozenset[str]  # scopes the user has enabled
    calls_last_hour: int  # executions of this tool in the last hour
    external_recipient: bool  # a recipient not in the user's contact allowlist
    destructive: bool  # code marked this specific call destructive
    quiet_hours: bool  # reserved for proactive jobs (R11); no row reads it yet
    user_confirmed: bool = False  # P3.2: a consumed approval for THIS payload
    step_up_confirmed: bool = False  # P3.2: TOTP step-up verified for it


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule_id: str
    reason: str
    step_up: bool = False  # TOTP step-up required for the approval (P3.2)

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError("unknown verdict")


class Policy(Protocol):
    """Pure and deterministic. No I/O, no model. Must never raise."""

    def decide(self, call: ProposedCall, ctx: PolicyContext) -> Decision: ...


# The only constructors of Decision. Positional so no `verdict=` keyword
# appears outside this file (the INV-5 scanner pins that).
def allow_decision(rule_id: str, reason: str, *, step_up: bool = False) -> Decision:
    return Decision("allow", rule_id, reason, step_up)


def approval_decision(rule_id: str, reason: str, *, step_up: bool) -> Decision:
    return Decision("require_approval", rule_id, reason, step_up)


def deny_decision(rule_id: str, reason: str) -> Decision:
    return Decision("deny", rule_id, reason, False)


def make_decision(verdict: Verdict, rule_id: str, reason: str, *, step_up: bool) -> Decision:
    return Decision(verdict, rule_id, reason, step_up and verdict != "deny")
