# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Google Calendar tool (T4.2). Per section 8.3's design rule -- "the cloud
model does execution" -- every action here routes through
`src.security.scrubber.execute_external` rather than calling the real
Google Calendar API directly, so this tool never becomes a second egress
point (INV-1).

No real Google API client is wired in (no OAuth flow, no
`google-api-python-client` dependency added -- that would need a licence
and CVE review per the operating manual, out of scope here). The scrubber
call itself reaches `settings.cloud_egress_url`, which in tests is a
stubbed/mocked endpoint, not a live Gemini/Google account -- this module
satisfies "stub the actual API, test the interface", not T4.2's full
acceptance criterion ("integration tests against real accounts").
"""

from typing import ClassVar

from src.contracts.tool_intent import Primitive, RiskClass, ToolIntent, ToolResult
from src.security.scrubber import execute_external
from src.tools.base import BaseTool

_ACTION_RISK: dict[str, RiskClass] = {
    "list": RiskClass.READ,
    "create": RiskClass.WRITE_REVERSIBLE,
    "update": RiskClass.WRITE_REVERSIBLE,
    "delete": RiskClass.DESTRUCTIVE,
}


class CalendarTool(BaseTool):
    name = "calendar"
    description = "List, create, update and delete Google Calendar events."
    risk_class = RiskClass.DESTRUCTIVE  # riskiest action this tool exposes
    schema: ClassVar[dict[str, dict[str, type]]] = {
        "list": {"start": str, "end": str},
        "create": {"title": str, "start": str, "end": str},
        "update": {"event_id": str, "title": str, "start": str, "end": str},
        "delete": {"event_id": str},
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
