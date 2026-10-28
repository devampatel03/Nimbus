# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Approval value types (pure).

`ConsumedApproval` is the ONLY thing that lets the dispatcher pass
`user_confirmed=True` to the policy (INV-5, P3.2). It is sealed: its
constructor refuses any caller that does not hold `_SEAL`, and the one function
that holds it, `seal_consumed`, is called only by `ApprovalStore.consume`
right after the atomic `pending -> consumed` UPDATE returned the row (pinned by
`tests/eval/invariants/test_inv5_approvals.py`). Model output, message text and
callback text can never produce one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

_SEAL = object()

# Action name for an approval that runs a model-proposed loop tool call.
TOOL_CALL_ACTION = "call"
# R8 (migration c4e8a1d92b37, `PgVectorMemoryStore.promote`): exact shape.
MEMORY_TOOL = "memory"
PROMOTE_ACTION = "promote"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """Plain JSON copy of a frozen payload (what a tool receives)."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [thaw(v) for v in value]
    return value


class ConsumedApproval:
    """Immutable, sealed, and deliberately NOT a dataclass, so
    `dataclasses.replace(approval, step_up_verified=True)` cannot mint a
    modified copy that passes the seal check."""

    __slots__ = (
        "action",
        "action_id",
        "payload",
        "payload_sha256",
        "source_event_id",
        "step_up_verified",
        "tool",
    )
    action_id: uuid.UUID
    tool: str
    action: str
    payload: Mapping[str, Any]
    payload_sha256: str
    step_up_verified: bool
    source_event_id: uuid.UUID | None

    def __init__(
        self,
        action_id: uuid.UUID,
        tool: str,
        action: str,
        payload: Mapping[str, Any],
        payload_sha256: str,
        step_up_verified: bool,
        source_event_id: uuid.UUID | None,
        seal: object,
    ) -> None:
        if seal is not _SEAL:
            raise TypeError("a ConsumedApproval comes only from a consumed pending_action row")
        if type(step_up_verified) is not bool:
            raise TypeError("step_up_verified must be a bool")
        for name, value in (
            ("action_id", action_id),
            ("tool", tool),
            ("action", action),
            ("payload", payload),
            ("payload_sha256", payload_sha256),
            ("step_up_verified", step_up_verified),
            ("source_event_id", source_event_id),
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ConsumedApproval is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ConsumedApproval is immutable")

    def __reduce__(self) -> Any:
        raise TypeError("ConsumedApproval cannot be copied or pickled")

    def __repr__(self) -> str:
        return f"ConsumedApproval(action_id={self.action_id}, tool={self.tool!r})"


def seal_consumed(
    *,
    action_id: uuid.UUID,
    tool: str,
    action: str,
    payload: Mapping[str, Any],
    payload_sha256: str,
    step_up_verified: bool,
    source_event_id: uuid.UUID | None,
) -> ConsumedApproval:
    return ConsumedApproval(
        action_id,
        tool,
        action,
        _freeze(payload),
        payload_sha256,
        step_up_verified,
        source_event_id,
        _SEAL,
    )


@dataclass(frozen=True)
class ApprovalAsk:
    """What the dispatcher hands over on `require_approval`: code facts plus
    the model-proposed call exactly as it would run."""

    tool: str
    args: Mapping[str, Any]
    step_up: bool
    tainted: bool
    rule_id: str


class AskOutcome(StrEnum):
    REQUESTED = "requested"  # a card was sent
    DUPLICATE = "duplicate"  # the same payload already has an open card
    TOO_MANY = "too_many"  # open-card cap reached
    INVALID = "invalid"  # payload not plain JSON / too large
    FAILED = "failed"  # storage or Telegram failure (row cancelled)
    UNAVAILABLE = "unavailable"  # step-up needed but no TOTP secret configured


@dataclass(frozen=True, kw_only=True)
class AuditRecord:
    """One `policy_audit` row. Names, ids and fixed codes only: never argument
    values, tokens, codes or secrets (INV-7)."""

    kind: str  # "decision" | "approval"
    event: str
    tool: str | None = None
    rule_id: str | None = None
    outcome: str | None = None
    reason: str | None = None
    tainted: bool | None = None
    step_up: bool | None = None
    executed: bool | None = None
    action_id: uuid.UUID | None = None
    totp_step: int | None = None
