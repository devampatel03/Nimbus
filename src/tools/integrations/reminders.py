# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Internal reminder store (T4.2 item 5, this session). Like `tasks.py`,
reminders never leave the device -- no third-party API, no cloud
reasoning needed to create/update/delete/list them -- so `run()` never
calls the scrubber and adds no egress point (INV-1 stays satisfied
trivially: there is nothing here to scan for).

Time-triggered via APScheduler's `AsyncIOScheduler` ("basic for now" per
this session's instruction): each reminder schedules a `date`-trigger job
that flips `fired: True` when `remind_at` elapses. The scheduler is
lazily created and started on first use inside `run()` (not in
`__init__`), mirroring the lazy-init pattern already used for
`Qwen3Embedder`/`VLLMClient` elsewhere in this codebase -- `ToolRegistry`
constructs every tool synchronously (`src/tools/registry.py`), which is
not guaranteed to run inside an active asyncio event loop, and
`AsyncIOScheduler.start()` needs one.

No notification delivery channel exists yet (push/SMS/etc. is a separate,
unbuilt concern) -- `fired` is the only observable signal today, polled
via `get_reminder`/`list_reminders` (same REST-read pattern as
`TasksTool.list_tasks`/`get_task` -- see that module's docstring for why
these are plain methods rather than `run()` actions).
"""

import uuid
from datetime import datetime
from typing import ClassVar

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.contracts.tool_intent import Primitive, RiskClass, ToolResult
from src.tools.base import BaseTool

_ACTION_RISK: dict[str, RiskClass] = {
    "create": RiskClass.WRITE_REVERSIBLE,
    "update": RiskClass.WRITE_REVERSIBLE,
    "delete": RiskClass.WRITE_REVERSIBLE,
    "list": RiskClass.READ,
    "get": RiskClass.READ,
}


class RemindersTool(BaseTool):
    name = "reminders"
    description = "Create, read, update and delete time-triggered reminders."
    risk_class = RiskClass.WRITE_REVERSIBLE
    schema: ClassVar[dict[str, dict[str, type]]] = {
        "create": {"title": str, "remind_at": str},
        "update": {"reminder_id": str, "title": str, "remind_at": str},
        "delete": {"reminder_id": str},
        "list": {},
        "get": {"reminder_id": str},
    }

    def __init__(self) -> None:
        self._reminders: dict[str, dict[str, Primitive]] = {}
        self._scheduler: AsyncIOScheduler | None = None

    def risk_class_for(self, action: str) -> RiskClass:
        try:
            return _ACTION_RISK[action]
        except KeyError:
            raise ValueError(f"{self.name}: unknown action {action!r}") from None

    def _ensure_scheduler(self) -> AsyncIOScheduler:
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler()
            self._scheduler.start()
        return self._scheduler

    def _fire(self, reminder_id: str) -> None:
        reminder = self._reminders.get(reminder_id)
        if reminder is not None:
            reminder["fired"] = True

    def _schedule(self, reminder_id: str, remind_at: str) -> None:
        self._ensure_scheduler().add_job(
            self._fire,
            "date",
            run_date=datetime.fromisoformat(remind_at),
            args=[reminder_id],
            id=reminder_id,
            replace_existing=True,
        )

    def list_reminders(self) -> list[dict[str, Primitive]]:
        return [dict(r) for r in self._reminders.values()]

    def get_reminder(self, reminder_id: str) -> dict[str, Primitive] | None:
        reminder = self._reminders.get(reminder_id)
        return dict(reminder) if reminder is not None else None

    def _not_found(self, action: str) -> ToolResult:
        return ToolResult(
            tool=self.name,
            action=action,
            status="error",
            result={},
            entity_refs={},
            error_message="reminder not found",
        )

    async def run(
        self, action: str, params: dict[str, Primitive], *, session_id: str
    ) -> ToolResult:
        self.validate_params(action, params)
        if action == "create":
            reminder_id = str(uuid.uuid4())
            remind_at = params["remind_at"]
            assert isinstance(remind_at, str)
            self._reminders[reminder_id] = {
                "reminder_id": reminder_id,
                "title": params["title"],
                "remind_at": remind_at,
                "fired": False,
            }
            self._schedule(reminder_id, remind_at)
            result: dict[str, Primitive] = {"reminder_id": reminder_id}
        elif action == "update":
            reminder_id_param = params["reminder_id"]
            if not isinstance(reminder_id_param, str) or reminder_id_param not in self._reminders:
                return self._not_found(action)
            remind_at = params["remind_at"]
            assert isinstance(remind_at, str)
            self._reminders[reminder_id_param]["title"] = params["title"]
            self._reminders[reminder_id_param]["remind_at"] = remind_at
            self._reminders[reminder_id_param]["fired"] = False
            self._schedule(reminder_id_param, remind_at)
            result = {"reminder_id": reminder_id_param}
        elif action == "delete":
            reminder_id_param = params["reminder_id"]
            if not isinstance(reminder_id_param, str) or reminder_id_param not in self._reminders:
                return self._not_found(action)
            del self._reminders[reminder_id_param]
            if self._scheduler is not None and self._scheduler.get_job(reminder_id_param):
                self._scheduler.remove_job(reminder_id_param)
            result = {"reminder_id": reminder_id_param}
        elif action == "get":
            reminder_id_param = params["reminder_id"]
            if not isinstance(reminder_id_param, str) or reminder_id_param not in self._reminders:
                return self._not_found(action)
            result = dict(self._reminders[reminder_id_param])
        elif action == "list":
            result = {"count": len(self._reminders)}
        else:
            raise ValueError(f"{self.name}: unknown action {action!r}")

        return ToolResult(
            tool=self.name, action=action, status="success", result=result, entity_refs={}
        )
