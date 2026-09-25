# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Gmail tool. Same intent-builder pattern as calendar.py (T4.2, "option A"
per this session's explicit instruction): every action ships a
`ToolIntent` to `src.security.scrubber.execute_external` and never calls
the Gmail API directly, so this tool adds no second egress point (INV-1)
and no OAuth client lives here -- see calendar.py's and scrubber.py's own
docstrings for the full "cloud model does execution" scope note.

`send` is a deliberate exception, per this session's explicit instruction
("dry-run it for now"): unlike every other action here, an email that has
actually been sent cannot be un-sent, so `run("send", ...)` validates and
builds the `ToolIntent` exactly like every other action but never calls
`execute_external` -- it returns a `status="dry_run"` `ToolResult` without
any network call. Route `send` through `execute_external` like the rest
of this tool once a live send has been explicitly signed off.
"""

from typing import ClassVar

from src.contracts.tool_intent import Primitive, RiskClass, ToolIntent, ToolResult
from src.security.scrubber import execute_external
from src.tools.base import BaseTool

_ACTION_RISK: dict[str, RiskClass] = {
    "read": RiskClass.READ,
    "search": RiskClass.READ,
    "draft": RiskClass.WRITE_REVERSIBLE,
    "send": RiskClass.WRITE_EXTERNAL,
}


class GmailTool(BaseTool):
    name = "gmail"
    description = "Read, search, draft and send email via Gmail."
    risk_class = RiskClass.WRITE_EXTERNAL  # riskiest action this tool exposes
    schema: ClassVar[dict[str, dict[str, type]]] = {
        "read": {"message_id": str},
        "search": {"query": str},
        "draft": {"to": str, "subject": str, "body": str},
        "send": {"to": str, "subject": str, "body": str},
    }

    def risk_class_for(self, action: str) -> RiskClass:
        try:
            return _ACTION_RISK[action]
        except KeyError:
            raise ValueError(f"{self.name}: unknown action {action!r}") from None

    async def run(
        self, action: str, params: dict[str, Primitive], *, session_id: str
    ) -> ToolResult:
        self.validate_params(action, params)
        intent = ToolIntent(
            tool=self.name,
            action=action,
            params=params,
            entity_refs={},
            risk_class=self.risk_class_for(action),
        )
        if action == "send":
            # Dry-run only this session (see module docstring): validated
            # and ready, but no network call is made.
            return ToolResult(
                tool=self.name,
                action=action,
                status="dry_run",
                result={"to": params["to"], "subject": params["subject"]},
                entity_refs={},
            )
        return await execute_external(intent, session_id)
