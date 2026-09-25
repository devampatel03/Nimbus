# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Google Drive tool. Read-only (T4.2 scope for this session: list/search/
read metadata, no write actions), same intent-builder pattern as
calendar.py/gmail.py: every action ships a `ToolIntent` to
`src.security.scrubber.execute_external`, no direct Drive API client, no
OAuth here (option A, this session's explicit instruction) -- see
calendar.py's and scrubber.py's docstrings for the full scope note.
"""

from typing import ClassVar

from src.contracts.tool_intent import Primitive, RiskClass, ToolIntent, ToolResult
from src.security.scrubber import execute_external
from src.tools.base import BaseTool

_ACTION_RISK: dict[str, RiskClass] = {
    "list": RiskClass.READ,
    "search": RiskClass.READ,
    "read": RiskClass.READ,
}


class DriveTool(BaseTool):
    name = "drive"
    description = "List, search and read file metadata in Google Drive."
    risk_class = RiskClass.READ
    schema: ClassVar[dict[str, dict[str, type]]] = {
        "list": {"folder_id": str},
        "search": {"query": str},
        "read": {"file_id": str},
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
        return await execute_external(intent, session_id)
