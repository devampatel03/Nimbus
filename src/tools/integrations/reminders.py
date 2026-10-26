# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Internal reminder store, as `LoopTool`s (Tool protocol v2, P2.5b). Like
`tasks.py`, reminders never leave the device: no third-party API, no network,
so this module imports no transport (INV-1). One tool per action, each with a
code-declared risk; outputs are JSON text, untrusted by default.

Time-triggered via APScheduler's `AsyncIOScheduler`: each reminder schedules a
`date`-trigger job that flips `fired: True` when `remind_at` elapses. The
scheduler is created and started lazily on first use inside a tool call, not in
`__init__`: `ToolRegistry` constructs groups synchronously, with no guaranteed
running event loop, and `AsyncIOScheduler.start()` needs one.

No notification channel exists yet; `fired` is the only observable signal,
read via `get_reminder`/`list_reminders`. Durable, IST-resolved scheduling with
catch-up replaces APScheduler in P3.3.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.orchestrator.dispatch import LoopTool, Risk, ToolError, ToolOutput
from src.tools.base import FunctionTool, ToolGroup, object_schema

Reminder = dict[str, str | bool]


def _parse(remind_at: str) -> datetime:
    try:
        return datetime.fromisoformat(remind_at)
    except ValueError:
        raise ToolError("remind_at must be an ISO-8601 datetime") from None


class RemindersTool(ToolGroup):
    name = "reminders"

    def __init__(self) -> None:
        self._reminders: dict[str, Reminder] = {}
        self._scheduler: AsyncIOScheduler | None = None

    def _ensure_scheduler(self) -> AsyncIOScheduler:
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler()
            self._scheduler.start()
        return self._scheduler

    def _fire(self, reminder_id: str) -> None:
        reminder = self._reminders.get(reminder_id)
        if reminder is not None:
            reminder["fired"] = True

    def _schedule(self, reminder_id: str, run_date: datetime) -> None:
        self._ensure_scheduler().add_job(
            self._fire,
            "date",
            run_date=run_date,
            args=[reminder_id],
            id=reminder_id,
            replace_existing=True,
        )

    def list_reminders(self) -> list[Reminder]:
        return [dict(r) for r in self._reminders.values()]

    def get_reminder(self, reminder_id: str) -> Reminder | None:
        reminder = self._reminders.get(reminder_id)
        return dict(reminder) if reminder is not None else None

    def _require(self, args: Mapping[str, Any]) -> Reminder:
        reminder = self._reminders.get(args["reminder_id"])
        if reminder is None:
            raise ToolError("reminder not found")
        return reminder

    async def _create(self, args: Mapping[str, Any]) -> ToolOutput:
        run_date = _parse(args["remind_at"])
        reminder_id = str(uuid.uuid4())
        self._reminders[reminder_id] = {
            "reminder_id": reminder_id,
            "title": args["title"],
            "remind_at": args["remind_at"],
            "fired": False,
        }
        self._schedule(reminder_id, run_date)
        return ToolOutput(json.dumps({"reminder_id": reminder_id}))

    async def _update(self, args: Mapping[str, Any]) -> ToolOutput:
        reminder = self._require(args)
        run_date = _parse(args["remind_at"])
        reminder["title"] = args["title"]
        reminder["remind_at"] = args["remind_at"]
        reminder["fired"] = False
        self._schedule(str(reminder["reminder_id"]), run_date)
        return ToolOutput(json.dumps({"reminder_id": reminder["reminder_id"]}))

    async def _delete(self, args: Mapping[str, Any]) -> ToolOutput:
        reminder = self._require(args)
        reminder_id = str(reminder["reminder_id"])
        del self._reminders[reminder_id]
        if self._scheduler is not None and self._scheduler.get_job(reminder_id):
            self._scheduler.remove_job(reminder_id)
        return ToolOutput(json.dumps({"reminder_id": reminder_id}))

    async def _get(self, args: Mapping[str, Any]) -> ToolOutput:
        return ToolOutput(json.dumps(self._require(args)))

    async def _list(self, args: Mapping[str, Any]) -> ToolOutput:
        return ToolOutput(
            json.dumps({"count": len(self._reminders), "reminders": self.list_reminders()})
        )

    def tools(self) -> list[LoopTool]:
        w, r = Risk.WRITE_INTERNAL, Risk.READ
        rid = {"reminder_id": "string"}
        return [
            FunctionTool(
                "reminders_create",
                "Create a reminder that fires at an ISO-8601 time.",
                w,
                object_schema({"title": "string", "remind_at": "string"}),
                self._create,
            ),
            FunctionTool(
                "reminders_update",
                "Change the title and time of a reminder.",
                w,
                object_schema({**rid, "title": "string", "remind_at": "string"}),
                self._update,
            ),
            FunctionTool(
                "reminders_delete", "Delete a reminder.", w, object_schema(rid), self._delete
            ),
            FunctionTool("reminders_get", "Read one reminder.", r, object_schema(rid), self._get),
            FunctionTool("reminders_list", "List all reminders.", r, object_schema({}), self._list),
        ]
