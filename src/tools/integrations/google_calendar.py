# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Google Calendar as `LoopTool`s (P3.5; ADR-0005, ADR-0009, ADR-0010).

Five tools, one per action, risk declared by code:

| tool             | risk        | policy                                        |
|------------------|-------------|-----------------------------------------------|
| calendar_list    | READ        | runs (scope `calendar` must be enabled)       |
| calendar_get     | READ        | runs                                          |
| calendar_create  | WRITE_EXTERNAL | approval card + TOTP step-up               |
| calendar_update  | WRITE_EXTERNAL | approval card + TOTP step-up               |
| calendar_delete  | DESTRUCTIVE | approval card + TOTP step-up                  |

Untrusted content (INV-6): titles, locations, descriptions, organizers and
attendees come from Google and may be authored by anyone who can invite the
user. They leave a tool ONLY in `ToolOutput.external` as `UntrustedText` (the
loop quarantines each item). `ToolOutput.text` carries counts, validated event
ids and code-generated times of the user's own writes, never Google text.

Event ids are never put in `ToolOutput.text`: the model gets opaque handles
(`HandleBook`) that code maps back to the real id before the policy sees the call.

Writes take absolute ISO-8601 datetimes with a UTC offset; the approval card
(`src.approvals.card`) shows them verbatim and in IST. No attendees parameter
exists and `sendUpdates=none` is always sent, so no invitation can be emailed.

`ToolRegistry` builds groups with no arguments; until `bind()` hands over the
Google client every tool fails closed ("unavailable"). The scope `calendar` is
not in `LOCAL_SCOPES`, so the policy denies these tools (`deny.scope_disabled`)
until the app enables it. Live use needs H4 (Google project, "In production").
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from src.core.clock import Clock
from src.core.timeparse import IST
from src.core.turn_context import current_event_id
from src.integrations.google.calendar_api import (
    MAX_LIST_RESULTS,
    CalendarClient,
    CalendarError,
    parse_instant,
    plan_create,
    plan_update,
    valid_event_id,
)
from src.orchestrator.dispatch import LoopTool, Risk, ToolError, ToolOutput
from src.tools.base import FunctionTool, ToolGroup, object_schema, validate_args

_TIME = (
    "ISO-8601 datetime with a UTC offset, India time is +05:30, "
    "e.g. 2026-10-30T15:00:00+05:30. Resolve relative dates yourself first."
)


def _guard(exc: CalendarError) -> ToolError:
    return ToolError(str(exc))


# Opaque handles (INV-6): a Google event id is third-party-authored text, so it
# never appears in ToolOutput.text. The model gets `h<list>_<n>` handles that
# code maps back to the real id BEFORE the policy and approval see the call
# (`resolve_args`), so the stored approval payload carries the real id and
# survives a restart, while a handle is only valid in this process.
HANDLE_RE = re.compile(r"h\d{1,9}_\d{1,2}")
MAX_HANDLES = 256
TARGET_KEY = "_target"  # code-written card line; model-supplied values are dropped


class HandleBook:
    def __init__(self) -> None:
        self._ids: OrderedDict[str, str] = OrderedDict()
        self._lists = 0

    def issue(self, event_ids: list[str]) -> list[str]:
        self._lists += 1
        handles = [f"h{self._lists}_{i}" for i, _ in enumerate(event_ids, 1)]
        self._ids.update(zip(handles, event_ids, strict=True))
        while len(self._ids) > MAX_HANDLES:
            self._ids.popitem(last=False)
        return handles

    def resolve(self, handle: object) -> str:
        if not isinstance(handle, str) or HANDLE_RE.fullmatch(handle) is None:
            raise ToolError("event_id must be a handle from calendar_list.")
        real = self._ids.get(handle)
        if real is None:
            raise ToolError("Unknown event handle; run calendar_list again.")
        return real


class _CalTool(FunctionTool):
    """`FunctionTool` plus the two dispatcher hooks (see `Dispatcher`):
    `resolve_args` (offline, before the policy) and `describe_args` (online,
    only when an approval card is about to be sent)."""

    def __init__(
        self,
        *a: Any,
        resolve: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        describe: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
    ) -> None:
        super().__init__(*a)
        self.resolve_args = self._resolve_async(resolve)
        if describe is not None:
            self.describe_args = describe

    @staticmethod
    def _resolve_async(
        fn: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]:
        async def go(args: Mapping[str, Any]) -> Mapping[str, Any]:
            return fn(args)

        return go

    async def run(self, args: Mapping[str, Any]) -> ToolOutput:
        clean = {k: v for k, v in args.items() if k != TARGET_KEY}
        validate_args(self.spec.parameters, clean)
        return await self._handler(clean)


class GoogleCalendarTool(ToolGroup):
    name = "calendar"

    def __init__(self) -> None:
        self._client: CalendarClient | None = None
        self._clock: Clock | None = None
        self._handles = HandleBook()

    def _resolve(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        """Model-path argument preparation: drop any model-supplied `_target`,
        and turn an event handle into the real id."""
        out = {k: v for k, v in args.items() if k != TARGET_KEY}
        if "event_id" in out:
            out["event_id"] = self._handles.resolve(out["event_id"])
        return out

    async def _describe(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        """Before an approval card: fetch the target and attach a clearly
        labelled line (the card shows it; the stored payload keeps it)."""
        client, _ = self._ready()
        label = await client.target_label(str(args.get("event_id", "")))
        return {**args, TARGET_KEY: label}

    def bind(self, client: CalendarClient, clock: Clock) -> None:
        self._client, self._clock = client, clock

    def _ready(self) -> tuple[CalendarClient, Clock]:
        if self._client is None or self._clock is None:
            raise ToolError("calendar is unavailable")
        return self._client, self._clock

    async def _list(self, args: Mapping[str, Any]) -> ToolOutput:
        client, clock = self._ready()
        try:
            now = clock.now()
            lo = (
                parse_instant(args["time_min"], field="time_min")
                if "time_min" in args
                else now.astimezone(IST)
            )
            hi = (
                parse_instant(args["time_max"], field="time_max")
                if "time_max" in args
                else lo + timedelta(days=7)
            )
            if hi <= lo:
                raise CalendarError("time_max must be after time_min.")
            limit = int(args.get("max_results", MAX_LIST_RESULTS))
            views, skipped = await client.list_events(lo, hi, limit)
        except CalendarError as exc:
            raise _guard(exc) from None
        handles = self._handles.issue([v.event_id for v in views])
        body = {"count": len(views), "events": handles, "skipped": skipped}
        return ToolOutput(json.dumps(body), external=tuple(v.external for v in views))

    async def _get(self, args: Mapping[str, Any]) -> ToolOutput:
        client, _ = self._ready()
        try:
            view = await client.get_event(valid_event_id(args["event_id"]))
        except CalendarError as exc:
            raise _guard(exc) from None
        return ToolOutput(json.dumps({"count": 1}), external=(view.external,))

    async def _create(self, args: Mapping[str, Any]) -> ToolOutput:
        client, clock = self._ready()
        try:
            plan = plan_create(args, now=clock.now(), source_event_id=current_event_id())
            created = await client.create_event(plan)
        except CalendarError as exc:
            raise _guard(exc) from None
        body = {
            "event_id": plan.event_id,
            "status": "created" if created else "already_created",
            "start": plan.start_ist,
            "end": plan.end_ist,
            "invitations_sent": False,
        }
        return ToolOutput(json.dumps(body), untrusted=False, external=())

    async def _update(self, args: Mapping[str, Any]) -> ToolOutput:
        client, clock = self._ready()
        try:
            plan = plan_update(args, now=clock.now())
            await client.update_event(plan)
        except CalendarError as exc:
            raise _guard(exc) from None
        body: dict[str, Any] = {"status": "updated"}
        if plan.start_ist:
            body |= {"start": plan.start_ist, "end": plan.end_ist}
        return ToolOutput(json.dumps(body), untrusted=False, external=())

    async def _delete(self, args: Mapping[str, Any]) -> ToolOutput:
        client, _ = self._ready()
        try:
            eid = valid_event_id(args["event_id"])
            deleted = await client.delete_event(eid)
        except CalendarError as exc:
            raise _guard(exc) from None
        status = "deleted" if deleted else "already_deleted"
        return ToolOutput(json.dumps({"status": status}), untrusted=False, external=())

    def tools(self) -> list[LoopTool]:
        """Offered to the model only while bound (and, via the policy table, only
        when scope `calendar` is enabled)."""
        return self.gate(self._all_tools(), lambda: self._client is not None)

    def _all_tools(self) -> list[LoopTool]:
        eid = {"event_id": "string"}
        fields = {"location": "string", "description": "string"}
        create_schema = object_schema(
            {"title": "string", "start": "string", "end": "string", **fields},
            required=("title", "start", "end"),
        )
        update_schema = object_schema(
            {**eid, "title": "string", "start": "string", "end": "string", **fields},
            required=("event_id",),
        )
        list_schema = object_schema(
            {"time_min": "string", "time_max": "string", "max_results": "integer"}, required=()
        )
        return [
            _CalTool(
                "calendar_list",
                "List up to 5 calendar events in a time range (default: next 7 days). "
                "Event text is returned as untrusted data. Times: " + _TIME,
                Risk.READ,
                list_schema,
                self._list,
                resolve=self._resolve,
            ),
            _CalTool(
                "calendar_get",
                "Read one calendar event by its handle from calendar_list.",
                Risk.READ,
                object_schema(eid),
                self._get,
                resolve=self._resolve,
            ),
            _CalTool(
                "calendar_create",
                "Create a calendar event. Needs the user's approval. No invitations are sent. "
                "start and end: " + _TIME,
                Risk.WRITE_EXTERNAL,
                create_schema,
                self._create,
                resolve=self._resolve,
            ),
            _CalTool(
                "calendar_update",
                "Change an event (handle from calendar_list): title, location, description or time (give start and end "
                "together). Needs the user's approval. No invitations are sent. Times: " + _TIME,
                Risk.WRITE_EXTERNAL,
                update_schema,
                self._update,
                resolve=self._resolve,
                describe=self._describe,
            ),
            _CalTool(
                "calendar_delete",
                "Delete a calendar event (handle from calendar_list). Needs the user's approval and authenticator code.",
                Risk.DESTRUCTIVE,
                object_schema(eid),
                self._delete,
                resolve=self._resolve,
                describe=self._describe,
            ),
        ]
