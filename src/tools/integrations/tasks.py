# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Internal task store, as `LoopTool`s (Tool protocol v2, P2.5b).

Tasks are Nimbus-local: no third-party API, no network, nothing leaves the
device. One tool per action so each carries its own code-declared risk
(`tasks_list`/`tasks_get` are READ; the rest WRITE_INTERNAL). Outputs are
JSON text and untrusted by default: a task title can originate from model or
external content, so it re-enters the prompt only as a DATA block.

Storage is a process-local dict. Durable, scheduled tasks arrive in P3.3.
`list_tasks`/`get_task` are plain accessors on the group for in-process readers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from src.orchestrator.dispatch import LoopTool, Risk, ToolError, ToolOutput
from src.tools.base import FunctionTool, ToolGroup, object_schema

Task = dict[str, str | bool]


class TasksTool(ToolGroup):
    name = "tasks"

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def list_tasks(self) -> list[Task]:
        return [dict(t) for t in self._tasks.values()]

    def get_task(self, task_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        return dict(task) if task is not None else None

    def _require(self, args: Mapping[str, Any]) -> Task:
        task = self._tasks.get(args["task_id"])
        if task is None:
            raise ToolError("task not found")
        return task

    async def _create(self, args: Mapping[str, Any]) -> ToolOutput:
        task_id = str(uuid.uuid4())
        self._tasks[task_id] = {"task_id": task_id, "title": args["title"], "done": False}
        return ToolOutput(json.dumps({"task_id": task_id}))

    async def _update(self, args: Mapping[str, Any]) -> ToolOutput:
        task = self._require(args)
        task["title"] = args["title"]
        return ToolOutput(json.dumps({"task_id": task["task_id"]}))

    async def _complete(self, args: Mapping[str, Any]) -> ToolOutput:
        task = self._require(args)
        task["done"] = True
        return ToolOutput(json.dumps({"task_id": task["task_id"]}))

    async def _delete(self, args: Mapping[str, Any]) -> ToolOutput:
        task = self._require(args)
        del self._tasks[str(task["task_id"])]
        return ToolOutput(json.dumps({"task_id": task["task_id"]}))

    async def _get(self, args: Mapping[str, Any]) -> ToolOutput:
        return ToolOutput(json.dumps(self._require(args)))

    async def _list(self, args: Mapping[str, Any]) -> ToolOutput:
        return ToolOutput(json.dumps({"count": len(self._tasks), "tasks": self.list_tasks()}))

    def tools(self) -> list[LoopTool]:
        w, r = Risk.WRITE_INTERNAL, Risk.READ
        tid = {"task_id": "string"}
        return [
            FunctionTool(
                "tasks_create",
                "Create a task in the internal task list.",
                w,
                object_schema({"title": "string"}),
                self._create,
            ),
            FunctionTool(
                "tasks_update",
                "Change the title of an existing task.",
                w,
                object_schema({**tid, "title": "string"}),
                self._update,
            ),
            FunctionTool(
                "tasks_complete", "Mark a task as done.", w, object_schema(tid), self._complete
            ),
            FunctionTool(
                "tasks_delete",
                "Delete a task from the internal list.",
                w,
                object_schema(tid),
                self._delete,
            ),
            FunctionTool("tasks_get", "Read one task.", r, object_schema(tid), self._get),
            FunctionTool("tasks_list", "List all tasks.", r, object_schema({}), self._list),
        ]
