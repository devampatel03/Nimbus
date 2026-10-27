# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Internal task list as `LoopTool`s (Tool protocol v2), persisted in Postgres
(P3.3). Tasks are Nimbus-local: no third-party API, no network. One tool per
action so each carries its own code-declared risk (`tasks_list`/`tasks_get` are
READ; the rest WRITE_INTERNAL). Outputs are JSON text and untrusted by default:
a task title can originate from model or external content, so it re-enters the
prompt only as a DATA block.

`ToolRegistry` builds groups with no arguments, so the group starts unbound and
every tool fails closed until `bind()` (done in `src.app.runtime`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.core.titles import TitleError, clean_title
from src.memory.schemas.scheduling import Task
from src.orchestrator.dispatch import LoopTool, Risk, ToolError, ToolOutput
from src.tools.base import FunctionTool, ToolGroup, object_schema

MAX_LISTED = 200


def _view(t: Task) -> dict[str, Any]:
    return {"task_id": str(t.id), "title": t.title, "done": t.done}


def _id(raw: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise ToolError("task not found") from None


class TasksTool(ToolGroup):
    name = "tasks"

    def __init__(self) -> None:
        self._sf: async_sessionmaker[AsyncSession] | None = None
        self._clock: Clock | None = None

    def bind(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._sf, self._clock = session_factory, clock

    def _ready(self) -> tuple[async_sessionmaker[AsyncSession], Clock]:
        if self._sf is None or self._clock is None:
            raise ToolError("tasks are unavailable")
        return self._sf, self._clock

    @staticmethod
    def _title(args: Mapping[str, Any]) -> str:
        try:
            return clean_title(args["title"])
        except TitleError as exc:
            raise ToolError(str(exc)) from None

    async def _create(self, args: Mapping[str, Any]) -> ToolOutput:
        sf, clock = self._ready()
        title = self._title(args)
        now = clock.now()
        async with sf() as s, s.begin():
            t = Task(id=uuid.uuid4(), title=title, done=False, created_at=now, updated_at=now)
            s.add(t)
        return ToolOutput(json.dumps({"task_id": str(t.id)}))

    async def _mutate(self, args: Mapping[str, Any], *, done: bool | None) -> ToolOutput:
        sf, clock = self._ready()
        tid = _id(args["task_id"])
        title = self._title(args) if done is None else None
        now = clock.now()
        async with sf() as s, s.begin():
            t = await s.get(Task, tid, with_for_update=True)
            if t is None:
                raise ToolError("task not found")
            if title is not None:
                t.title = title
            if done is not None:
                t.done = done
                t.done_at = now if done else None
            t.updated_at = now
        return ToolOutput(json.dumps({"task_id": str(tid)}))

    async def _update(self, args: Mapping[str, Any]) -> ToolOutput:
        return await self._mutate(args, done=None)

    async def _complete(self, args: Mapping[str, Any]) -> ToolOutput:
        return await self._mutate(args, done=True)

    async def _delete(self, args: Mapping[str, Any]) -> ToolOutput:
        sf, _ = self._ready()
        tid = _id(args["task_id"])
        async with sf() as s, s.begin():
            res = await s.execute(delete(Task).where(Task.id == tid).returning(Task.id))
            if res.scalar_one_or_none() is None:
                raise ToolError("task not found")
        return ToolOutput(json.dumps({"task_id": str(tid)}))

    async def _get(self, args: Mapping[str, Any]) -> ToolOutput:
        sf, _ = self._ready()
        async with sf() as s:
            t = await s.get(Task, _id(args["task_id"]))
        if t is None:
            raise ToolError("task not found")
        return ToolOutput(json.dumps(_view(t)))

    async def _list(self, args: Mapping[str, Any]) -> ToolOutput:
        sf, _ = self._ready()
        async with sf() as s:
            rows = list(
                await s.scalars(select(Task).order_by(Task.created_at, Task.id).limit(MAX_LISTED))
            )
        return ToolOutput(json.dumps({"count": len(rows), "tasks": [_view(t) for t in rows]}))

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
