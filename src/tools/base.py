# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""BaseTool: the interface every tool integration implements
(docs/TECHNICAL_ARCHITECTURE.md section 9.1, ACT-3 risk model).

`risk_class` is declared per-tool-instance (not per-action) deliberately:
section 9.1's table gates by example action ("send email" vs "list
calendar"), so a tool whose actions span more than one risk tier declares
the tier of its *riskiest* action and exposes `risk_class_for(action)` for
callers (the orchestrator's `act` node, the approver) that need the
per-action distinction.

`schema` describes the primitive params each action accepts -- this is
what `src/tools/registry.py` and the orchestrator use to validate a
proposed `ToolIntent.params` shape before the tool runs. (The old
scrubber `ALLOWED_TOOLS` whitelist was deleted with `src/security` in
P1.4c; Tool protocol v2 lands in P2.5b.)
"""

from abc import ABC, abstractmethod
from typing import ClassVar

from src.contracts.tool_intent import Primitive, RiskClass, ToolResult


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    risk_class: ClassVar[RiskClass]
    # action -> {param_name: expected primitive type}
    schema: ClassVar[dict[str, dict[str, type]]]

    def risk_class_for(self, action: str) -> RiskClass:
        """Per-action override point. Default: the tool's declared tier."""
        return self.risk_class

    def validate_params(self, action: str, params: dict[str, Primitive]) -> None:
        action_schema = self.schema.get(action)
        if action_schema is None:
            raise ValueError(f"{self.name}: unknown action {action!r}")
        for key, value in params.items():
            expected = action_schema.get(key)
            if expected is None:
                raise ValueError(f"{self.name}.{action}: unexpected param {key!r}")
            if value is not None and not isinstance(value, expected):
                raise ValueError(
                    f"{self.name}.{action}: param {key!r} expected {expected}, got {type(value)}"
                )

    @abstractmethod
    async def run(
        self, action: str, params: dict[str, Primitive], *, session_id: str
    ) -> ToolResult:
        """Execute `action`. A tool that needs a third-party service must
        use `src.net` (the only module allowed to import a transport, INV-1)
        and never import a transport library itself. Purely local tools
        (tasks, reminders) ignore `session_id`.
        """
        raise NotImplementedError
