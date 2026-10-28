# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Reminders as `LoopTool`s (Tool protocol v2), persisted in Postgres (P3.3).

Reminders are stored and scheduled in ONE transaction (`ReminderStore`) and
delivered by the job runner, so they survive restarts and a downtime catches up
once (R1, R11). Times are resolved in Asia/Kolkata by `src.core.timeparse` and
the resolved ABSOLUTE time is returned to the model/user in every create and
update (`confirmation`). Local only: no network, no transport import (INV-1).

`ToolRegistry` builds groups with no arguments, so the group starts unbound and
every tool fails closed ("unavailable") until `bind()` hands it the store (done
in `src.app.runtime`). Writes are still gated by the policy (INV-5); delivery is
not a tool and never consults the model.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from src.core.clock import Clock
from src.core.timeparse import ResolvedTime, TimeParseError, confirmation_text, parse_when
from src.core.titles import TitleError, clean_title
from src.core.turn_context import current_event_id
from src.orchestrator.dispatch import LoopTool, Risk, ToolError, ToolOutput
from src.scheduler.reminders import ReminderStore, reminder_view
from src.tools.base import FunctionTool, ToolGroup, object_schema


def _id(raw: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise ToolError("reminder not found") from None


class RemindersTool(ToolGroup):
    name = "reminders"

    def __init__(self) -> None:
        self._store: ReminderStore | None = None
        self._clock: Clock | None = None

    def bind(self, store: ReminderStore, clock: Clock) -> None:
        self._store, self._clock = store, clock

    def _ready(self) -> tuple[ReminderStore, Clock]:
        if self._store is None or self._clock is None:
            raise ToolError("reminders are unavailable")
        return self._store, self._clock

    @staticmethod
    def _title(args: Mapping[str, Any]) -> str:
        try:
            return clean_title(args["title"])
        except TitleError as exc:
            raise ToolError(str(exc)) from None

    @staticmethod
    def _when(args: Mapping[str, Any], clock: Clock) -> ResolvedTime:
        try:
            return parse_when(args["remind_at"], now=clock.now())
        except TimeParseError as exc:
            raise ToolError(str(exc)) from None

    async def _create(self, args: Mapping[str, Any]) -> ToolOutput:
        store, clock = self._ready()
        title = self._title(args)
        when = self._when(args, clock)
        r = await store.create(title, when.at, source_event_id=current_event_id())
        body = reminder_view(r) | {"confirmation": confirmation_text(when, now=clock.now())}
        return ToolOutput(json.dumps(body))

    async def _update(self, args: Mapping[str, Any]) -> ToolOutput:
        store, clock = self._ready()
        rid = _id(args["reminder_id"])
        title = self._title(args)
        when = self._when(args, clock)
        r = await store.update(rid, title, when.at)
        if r is None:
            raise ToolError("reminder not found")
        body = reminder_view(r) | {"confirmation": confirmation_text(when, now=clock.now())}
        return ToolOutput(json.dumps(body))

    async def _delete(self, args: Mapping[str, Any]) -> ToolOutput:
        store, _ = self._ready()
        rid = _id(args["reminder_id"])
        if not await store.delete(rid):
            raise ToolError("reminder not found")
        return ToolOutput(json.dumps({"reminder_id": str(rid)}))

    async def _get(self, args: Mapping[str, Any]) -> ToolOutput:
        store, _ = self._ready()
        r = await store.get(_id(args["reminder_id"]))
        if r is None:
            raise ToolError("reminder not found")
        return ToolOutput(json.dumps(reminder_view(r)))

    async def _list(self, args: Mapping[str, Any]) -> ToolOutput:
        store, _ = self._ready()
        rows = [reminder_view(r) for r in await store.list()]
        return ToolOutput(json.dumps({"count": len(rows), "reminders": rows}))

    def tools(self) -> list[LoopTool]:
        w, r = Risk.WRITE_INTERNAL, Risk.READ
        rid = {"reminder_id": "string"}
        when = (
            "Time in India time (IST): an ISO-8601 datetime, or a phrase such as "
            "'tomorrow 9am', 'in 2 hours', 'friday 6pm', '5 Nov 7pm'."
        )
        return [
            FunctionTool(
                "reminders_create",
                "Create a reminder. " + when + " The reply confirms the absolute time.",
                w,
                object_schema({"title": "string", "remind_at": "string"}),
                self._create,
            ),
            FunctionTool(
                "reminders_update",
                "Change the title and time of a reminder. " + when,
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
