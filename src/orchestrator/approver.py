# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Isolated approval agent (docs/TECHNICAL_ARCHITECTURE.md 9.2, ACT-4/ACT-5).

Gates WRITE_EXTERNAL and DESTRUCTIVE tool calls. Isolation is structural,
not conventional: `review`'s signature is `(proposed_action, original_intent,
risk_class)` -- it has no parameter through which `conversation_history`,
`retrieved_memories`, or `personality_slice` could reach it, so a caller
cannot accidentally leak tainted content into the approver's context by
passing the wrong object; there is no object shaped like `AgentState` this
function will accept.

`Approver.tools` is a `ClassVar[tuple[()]] = ()` -- ACT-5 requires an
empty tool list, asserted in `tests/unit/test_approver.py`, not merely
assumed by inspection.

No cloud/local-model judgement is wired in yet (same constraint as
`check_friction`, AD-11): this is a deterministic heuristic reviewer, not
the eventual LLM-based approver. It exists to prove the gating structure
(isolation, empty tool list, per-risk-tier gate) works end-to-end; a real
model-backed approver is a follow-up, not this task's scope.
"""

from dataclasses import dataclass
from typing import ClassVar

from src.contracts.tool_intent import Primitive, RiskClass

_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard the above",
    "disregard previous",
    "new instructions:",
    "system:",
)


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reason: str


class Approver:
    """ACT-5: no tools, ever. Asserted by test, not assumed."""

    tools: ClassVar[tuple[()]] = ()

    def review(
        self,
        *,
        proposed_action: dict[str, Primitive],
        original_intent: str,
        risk_class: RiskClass,
    ) -> ApprovalDecision:
        scannable = [original_intent] + [
            value for value in proposed_action.values() if isinstance(value, str)
        ]
        lowered = " ".join(scannable).lower()
        if any(marker in lowered for marker in _INJECTION_MARKERS):
            return ApprovalDecision(
                approved=False,
                reason="rejected: possible instruction injection in proposed action",
            )

        if risk_class == RiskClass.DESTRUCTIVE:
            if proposed_action.get("user_confirmed") is not True:
                return ApprovalDecision(
                    approved=False,
                    reason="rejected: destructive actions require explicit user confirmation",
                )
            return ApprovalDecision(approved=True, reason="approved: destructive, user confirmed")

        if risk_class == RiskClass.WRITE_EXTERNAL:
            return ApprovalDecision(
                approved=True, reason="approved: write_external, no injection markers found"
            )

        return ApprovalDecision(
            approved=True, reason=f"{risk_class.value} does not require approval"
        )
