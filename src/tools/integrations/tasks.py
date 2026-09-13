# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Internal task store (T4.2 scope note: this tool is Nimbus-local, not a
Google integration). Tasks never leave the device -- no third-party API,
no cloud reasoning needed to create/update/complete/delete/list them --
so `run()` never calls the scrubber and adds no egress point (INV-1 stays
satisfied trivially: there is nothing here to scan for).

Storage is a process-local dict, not Postgres: a durable task store is a
`src/memory` / P-MEMORY concern (out of scope for this tool-registry
task); this is the minimal implementation needed to exercise the
registry/risk-gating machinery with a genuinely local, non-cloud tool.

`list_tasks`/`get_task` (plain methods, not `run()` actions) exist for
`src/api/routes/tasks.py`'s REST reads: `ToolResult.result` is one of the
six frozen `src/contracts/` shapes (`dict[str, Primitive]`, no nested
lists/dicts -- SCR-2's closed-schema intent for the scrubber path), so a
full task list or task record can't travel through `run()`'s return value
at all. Direct, in-process method calls on the same `TasksTool` instance
the orchestrator's `act` node already uses aren't constrained by that
contract (it exists to bound what a `ToolIntent`/`ToolResult` can carry
across the scrubber boundary, not every in-process call into a local
tool), so the REST layer reads through these instead of `run()`.
"""

import uuid
from typing import ClassVar

from src.contracts.tool_intent import Primitive, RiskClass, ToolResult
from src.tools.base import BaseTool

_ACTION_RISK: dict[str, RiskClass] = {
    "create": RiskClass.WRITE_REVERSIBLE,
    "update": RiskClass.WRITE_REVERSIBLE,
    "complete": RiskClass.WRITE_REVERSIBLE,
    "delete": RiskClass.WRITE_REVERSIBLE,  # internal-only state, unlike calendar's DESTRUCTIVE delete
    "list": RiskClass.READ,
    "get": RiskClass.READ,
}


class TasksTool(BaseTool):
    name = "tasks"
    description = "Create, read, update, complete and delete items in the internal task store."
    risk_class = RiskClass.WRITE_REVERSIBLE
    schema: ClassVar[dict[str, dict[str, type]]] = {
        "create": {"title": str},
        "update": {"task_id": str, "title": str},
        "complete": {"task_id": str},
        "delete": {"task_id": str},
        "list": {},
        "get": {"task_id": str},
    }

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Primitive]] = {}

    def risk_class_for(self, action: str) -> RiskClass:
        try:
            return _ACTION_RISK[action]
        except KeyError:
            raise ValueError(f"{self.name}: unknown action {action!r}") from None

    def list_tasks(self) -> list[dict[str, Primitive]]:
        return [dict(task) for task in self._tasks.values()]

    def get_task(self, task_id: str) -> dict[str, Primitive] | None:
        task = self._tasks.get(task_id)
        return dict(task) if task is not None else None

    def _not_found(self, action: str) -> ToolResult:
        return ToolResult(
            tool=self.name,
            action=action,
            status="error",
            result={},
            entity_refs={},
            error_message="task not found",
        )

    async def run(
        self, action: str, params: dict[str, Primitive], *, session_id: str
    ) -> ToolResult:
        self.validate_params(action, params)
        if action == "create":
            task_id = str(uuid.uuid4())
            self._tasks[task_id] = {"task_id": task_id, "title": params["title"], "done": False}
            result: dict[str, Primitive] = {"task_id": task_id}
        elif action == "update":
            task_id_param = params["task_id"]
            if not isinstance(task_id_param, str) or task_id_param not in self._tasks:
                return self._not_found(action)
            self._tasks[task_id_param]["title"] = params["title"]
            result = {"task_id": task_id_param}
        elif action == "complete":
            task_id_param = params["task_id"]
            if not isinstance(task_id_param, str) or task_id_param not in self._tasks:
                return self._not_found(action)
            self._tasks[task_id_param]["done"] = True
            result = {"task_id": task_id_param}
        elif action == "delete":
            task_id_param = params["task_id"]
            if not isinstance(task_id_param, str) or task_id_param not in self._tasks:
                return self._not_found(action)
            del self._tasks[task_id_param]
            result = {"task_id": task_id_param}
        elif action == "get":
            task_id_param = params["task_id"]
            if not isinstance(task_id_param, str) or task_id_param not in self._tasks:
                return self._not_found(action)
            result = dict(self._tasks[task_id_param])
        elif action == "list":
            result = {"count": len(self._tasks)}
        else:
            raise ValueError(f"{self.name}: unknown action {action!r}")

        return ToolResult(
            tool=self.name, action=action, status="success", result=result, entity_refs={}
        )
