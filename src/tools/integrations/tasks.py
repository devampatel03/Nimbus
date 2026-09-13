# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Internal task store (T4.2 scope note: this tool is Nimbus-local, not a
Google integration). Tasks never leave the device -- no third-party API,
no cloud reasoning needed to create/complete/list them -- so `run()` never
calls the scrubber and adds no egress point (INV-1 stays satisfied
trivially: there is nothing here to scan for).

Storage is a process-local dict, not Postgres: a durable task store is a
`src/memory` / P-MEMORY concern (out of scope for this tool-registry
task); this is the minimal implementation needed to exercise the
registry/risk-gating machinery with a genuinely local, non-cloud tool.
"""

import uuid
from typing import ClassVar

from src.contracts.tool_intent import Primitive, RiskClass, ToolResult
from src.tools.base import BaseTool

_ACTION_RISK: dict[str, RiskClass] = {
    "create": RiskClass.WRITE_REVERSIBLE,
    "complete": RiskClass.WRITE_REVERSIBLE,
    "list": RiskClass.READ,
}


class TasksTool(BaseTool):
    name = "tasks"
    description = "Create, complete and list items in the internal task store."
    risk_class = RiskClass.WRITE_REVERSIBLE
    schema: ClassVar[dict[str, dict[str, type]]] = {
        "create": {"title": str},
        "complete": {"task_id": str},
        "list": {},
    }

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Primitive]] = {}

    def risk_class_for(self, action: str) -> RiskClass:
        try:
            return _ACTION_RISK[action]
        except KeyError:
            raise ValueError(f"{self.name}: unknown action {action!r}") from None

    async def run(
        self, action: str, params: dict[str, Primitive], *, session_id: str
    ) -> ToolResult:
        self.validate_params(action, params)
        if action == "create":
            task_id = str(uuid.uuid4())
            self._tasks[task_id] = {"task_id": task_id, "title": params["title"], "done": False}
            result: dict[str, Primitive] = {"task_id": task_id}
        elif action == "complete":
            task_id_param = params["task_id"]
            if not isinstance(task_id_param, str) or task_id_param not in self._tasks:
                return ToolResult(
                    tool=self.name,
                    action=action,
                    status="error",
                    result={},
                    entity_refs={},
                    error_message="task not found",
                )
            self._tasks[task_id_param]["done"] = True
            result = {"task_id": task_id_param}
        elif action == "list":
            result = {"count": len(self._tasks)}
        else:
            raise ValueError(f"{self.name}: unknown action {action!r}")

        return ToolResult(
            tool=self.name, action=action, status="success", result=result, entity_refs={}
        )
