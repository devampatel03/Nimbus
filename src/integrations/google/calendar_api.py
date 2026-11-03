# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Google Calendar v3 over the egress client (P3.5, R13).

Only `src.net` talks to the network: every call is `EgressClient.request` with
`Service.GOOGLE_APIS` (`www.googleapis.com`, already allow-listed by ADR-0004;
no new host or Service, so no ADR is needed). The only calendar is `primary`
and the only scope is `calendar.events` (minimum for read + write of events).

Safety properties, all enforced here and tested with recorded fixtures:
* `sendUpdates=none` on every write: Nimbus never makes Google email an
  invitation. There is no attendees parameter at all.
* Event ids are derived deterministically (`derive_event_id`) from the turn's
  source event, so a replayed or retried create hits Google's `409` and is
  reported as already done instead of duplicating.
* Times are resolved to absolute Asia/Kolkata datetimes BEFORE anything is
  sent; `EventPlan` carries both the ISO form and the IST form that the
  approval card and the tool reply show.
* Everything Google returns that a third party can author (title, location,
  description, organizer, attendees) is sanitised (HTML stripped, hidden
  characters dropped, size capped) and handed back only as `UntrustedText`.
  Error messages are fixed strings and never echo response bodies.

R15: the OAuth consent screen must be "In production" or refresh tokens expire
after 7 days (see `src.integrations.google.auth`). Live use needs H4.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from src.core.timeparse import IST, format_ist, parse_aware_iso
from src.integrations.google.auth import (
    SCOPE_CALENDAR_EVENTS,
    GoogleAuth,
    GoogleAuthError,
)
from src.lanes.data import SourceKind, UntrustedText
from src.net import EgressClient, EgressError
from src.net.policy import Service

CALENDAR_ID = "primary"
_BASE = f"/calendar/v3/calendars/{CALENDAR_ID}/events"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_LIST_RESULTS = 5  # equals MAX_EXTERNAL_ITEMS: one quarantine call per event
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 2000
MAX_FIELD_CHARS = 300
MAX_DURATION = timedelta(days=7)
MAX_HORIZON = timedelta(days=5 * 365)
# Google event ids: base32hex alphabet, 5..1024 chars. We only accept (and
# emit) a tight subset, so an attacker-authored id cannot carry text.
_EVENT_ID_RE = re.compile(r"[a-v0-9]{5,64}")
_TAG_RE = re.compile(r"<[^>]{0,2000}>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class CalendarError(Exception):
    """Sanitised failure; the message is fixed text, safe to show the model."""

    def __init__(self, message: str, *, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


# -- sanitising --------------------------------------------------------------


def clean_external(raw: object, limit: int) -> str:
    """Strip HTML and hidden text from third-party text and cap its size."""
    if not isinstance(raw, str):
        return ""
    text = _COMMENT_RE.sub(" ", raw[: limit * 8])
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", html.unescape(text))
    kept: list[str] = []
    for c in text:
        cat = unicodedata.category(c)
        if c == "\n":
            kept.append(c)
        elif c in "\r\t":
            kept.append(" ")
        elif cat[0] == "C" or cat in ("Zl", "Zp"):
            continue  # control, format (zero-width, bidi), private use
        else:
            kept.append(c)
    out = re.sub(r" {2,}", " ", "".join(kept))
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out[:limit]


def valid_event_id(raw: object) -> str:
    if not isinstance(raw, str) or not _EVENT_ID_RE.fullmatch(raw):
        raise CalendarError("That event id is not valid.", code="bad_id")
    return raw


# -- time and id resolution (pure) -------------------------------------------


def parse_instant(raw: object, *, field: str) -> datetime:
    """An ISO-8601 datetime WITH an explicit UTC offset, as an IST datetime.
    Naive times are refused: relative dates are resolved to absolute times
    before the call, and the approval card shows them verbatim."""
    dt = parse_aware_iso(raw)
    if dt is None:
        raise CalendarError(
            f"{field} must be an ISO-8601 datetime with a UTC offset (India time is +05:30), "
            "e.g. 2026-10-30T15:00:00+05:30."
        )
    return dt.astimezone(IST)


def derive_event_id(source_event_id: uuid.UUID | None, title: str, start: str, end: str) -> str:
    """Deterministic Google event id: the same turn proposing the same event
    always gets the same id, so a retry cannot duplicate it."""
    seed = f"nimbus-cal-v1|{source_event_id or 'none'}|{title}|{start}|{end}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()[:16]
    return base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()


@dataclass(frozen=True)
class EventPlan:
    """A fully resolved write, before anything is sent."""

    event_id: str
    body: Mapping[str, Any]
    start_iso: str
    end_iso: str
    start_ist: str
    end_ist: str


def _window(start: object, end: object, now: datetime) -> tuple[datetime, datetime]:
    s, e = parse_instant(start, field="start"), parse_instant(end, field="end")
    if e <= s:
        raise CalendarError("end must be after start.")
    if e - s > MAX_DURATION:
        raise CalendarError("An event can be at most 7 days long.")
    if s > now.astimezone(IST) + MAX_HORIZON:
        raise CalendarError("That date is more than five years away.")
    return s, e


def _text(raw: object, limit: int, *, field: str, required: bool = False) -> str:
    if not isinstance(raw, str):
        raise CalendarError(f"{field} must be text.")
    cleaned = "".join(
        " " if c in "\r\t" else c for c in raw if c in "\r\n\t" or unicodedata.category(c)[0] != "C"
    ).strip()
    if required and not cleaned:
        raise CalendarError(f"{field} is required.")
    if len(cleaned) > limit:
        raise CalendarError(f"{field} is too long (limit {limit} characters).")
    return cleaned


def _dt_body(dt: datetime) -> dict[str, str]:
    return {"dateTime": dt.isoformat(), "timeZone": "Asia/Kolkata"}


def _title(raw: object) -> str:
    return " ".join(_text(raw, MAX_TITLE_CHARS, field="title", required=True).split())


def plan_create(
    args: Mapping[str, Any], *, now: datetime, source_event_id: uuid.UUID | None
) -> EventPlan:
    title = _title(args["title"])
    s, e = _window(args["start"], args["end"], now)
    body: dict[str, Any] = {"summary": title, "start": _dt_body(s), "end": _dt_body(e)}
    if "location" in args:
        body["location"] = _text(args["location"], MAX_FIELD_CHARS, field="location")
    if "description" in args:
        body["description"] = _text(args["description"], MAX_DESCRIPTION_CHARS, field="description")
    eid = derive_event_id(source_event_id, title, s.isoformat(), e.isoformat())
    body["id"] = eid
    return EventPlan(eid, body, s.isoformat(), e.isoformat(), format_ist(s), format_ist(e))


def plan_update(args: Mapping[str, Any], *, now: datetime) -> EventPlan:
    """A PATCH body with only the fields supplied. Time moves need both start
    and end (no half-specified windows)."""
    eid = valid_event_id(args["event_id"])
    body: dict[str, Any] = {}
    if "title" in args:
        body["summary"] = _title(args["title"])
    if "location" in args:
        body["location"] = _text(args["location"], MAX_FIELD_CHARS, field="location")
    if "description" in args:
        body["description"] = _text(args["description"], MAX_DESCRIPTION_CHARS, field="description")
    if ("start" in args) != ("end" in args):
        raise CalendarError("To move an event give both start and end.")
    s_iso = e_iso = s_ist = e_ist = ""
    if "start" in args:
        s, e = _window(args["start"], args["end"], now)
        body["start"], body["end"] = _dt_body(s), _dt_body(e)
        s_iso, e_iso, s_ist, e_ist = s.isoformat(), e.isoformat(), format_ist(s), format_ist(e)
    if not body:
        raise CalendarError("Nothing to change: give a title, location, description or times.")
    return EventPlan(eid, body, s_iso, e_iso, s_ist, e_ist)


# -- reading Google's answer -------------------------------------------------


@dataclass(frozen=True)
class EventView:
    """One event from Google. `event_id` is validated; `external` carries all
    third-party text and is the only form in which it may leave this module."""

    event_id: str
    external: UntrustedText
    # Structured copies for code-built summaries (P5.1). `title` is third-party text:
    # clean_external-sanitised, still UNTRUSTED; render it only as quoted data.
    title: str = ""
    start_at: datetime | None = None  # None for all-day or unparseable
    all_day: bool = False


def _when(node: object) -> str:
    if not isinstance(node, dict):
        return ""
    raw = node.get("dateTime") or node.get("date")
    if not isinstance(raw, str) or len(raw) > 40:
        return ""
    try:
        if "T" in raw:
            at = datetime.fromisoformat(raw)
            if at.tzinfo is None or at.utcoffset() is None:
                return ""  # naive: never guess (host-local would be wrong); show nothing
            return format_ist(at)
        return datetime.fromisoformat(raw).strftime("%a %d %b %Y (all day)")
    except ValueError:
        return ""


def _start(node: object) -> tuple[datetime | None, bool]:
    if not isinstance(node, dict):
        return None, False
    raw = node.get("dateTime") or node.get("date")
    if not isinstance(raw, str) or len(raw) > 40:
        return None, False
    try:
        if "T" in raw:
            at = datetime.fromisoformat(raw)
            if at.tzinfo is None or at.utcoffset() is None:
                return None, False
            return at, False
        datetime.fromisoformat(raw)
        return None, True
    except ValueError:
        return None, False


def to_view(item: object) -> EventView | None:
    """Parse one Google event resource. None when it has no usable id."""
    if not isinstance(item, dict):
        return None
    try:
        eid = valid_event_id(item.get("id"))
    except CalendarError:
        return None
    organizer = item.get("organizer")
    org = clean_external(organizer.get("email") if isinstance(organizer, dict) else "", 120)
    raw_att = item.get("attendees")
    att: list[str] = []
    if isinstance(raw_att, list):
        for a in raw_att[:20]:
            if isinstance(a, dict):
                name = clean_external(a.get("displayName") or a.get("email") or "", 120)
                if name:
                    att.append(name)
    lines = [
        f"event_id: {eid}",
        f"start: {_when(item.get('start'))}",
        f"end: {_when(item.get('end'))}",
        f"title: {clean_external(item.get('summary'), MAX_TITLE_CHARS)}",
        f"location: {clean_external(item.get('location'), MAX_FIELD_CHARS)}",
        f"organizer: {org}",
        f"attendees: {', '.join(att)}",
        f"description: {clean_external(item.get('description'), MAX_DESCRIPTION_CHARS)}",
    ]
    start_at, all_day = _start(item.get("start"))
    return EventView(
        eid,
        UntrustedText("\n".join(lines), source=SourceKind.CALENDAR_INVITE),
        title=clean_external(item.get("summary"), MAX_TITLE_CHARS),
        start_at=start_at,
        all_day=all_day,
    )


# -- the HTTP client ---------------------------------------------------------

_FIXED_ERRORS: Mapping[int, tuple[str, str]] = {
    400: ("Google rejected the request.", "bad_request"),
    401: ("Google Calendar access was refused; re-authorisation may be needed.", "unauthorized"),
    403: ("Google Calendar denied permission for this action.", "forbidden"),
    404: ("That calendar event was not found.", "not_found"),
    409: ("That calendar event already exists.", "conflict"),
    410: ("That calendar event was already deleted.", "gone"),
    429: ("Google Calendar is rate limiting requests; try again later.", "rate_limited"),
}


class CalendarClient:
    def __init__(self, egress: EgressClient, auth: GoogleAuth) -> None:
        self._egress = egress
        self._auth = auth

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            token = await self._auth.access_token([SCOPE_CALENDAR_EVENTS])
        except GoogleAuthError:
            raise CalendarError(
                "Google access needs to be re-authorised (run scripts/google_oauth.py).",
                code="reauth",
            ) from None
        headers = {
            "authorization": f"Bearer {token.get_secret_value()}",
            "accept": "application/json",
        }
        try:
            resp = await self._egress.request(
                Service.GOOGLE_APIS,
                method,
                path,
                params=dict(params) if params else None,
                headers=headers,
                json=dict(body) if body is not None else None,
                max_bytes=MAX_RESPONSE_BYTES,
            )
        except EgressError:
            raise CalendarError("Google Calendar could not be reached.", code="network") from None
        if resp.is_success:
            try:
                return resp.json() if resp.content else {}
            except ValueError:
                raise CalendarError("Google Calendar sent an unreadable reply.") from None
        if resp.status_code >= 500:
            raise CalendarError("Google Calendar is having problems; try again later.", code="5xx")
        msg, code = _FIXED_ERRORS.get(
            resp.status_code, ("Google Calendar request failed.", "error")
        )
        raise CalendarError(msg, code=code)

    async def list_events(
        self, time_min: datetime, time_max: datetime, limit: int
    ) -> tuple[list[EventView], int]:
        """The usable events and how many returned items were skipped (ids
        outside the strict pattern, e.g. recurring-instance ids)."""
        data = await self._call(
            "GET",
            _BASE,
            params={
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": max(1, min(limit, MAX_LIST_RESULTS)),
            },
        )
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return [], 0
        seen = items[:MAX_LIST_RESULTS]
        views = [v for v in (to_view(i) for i in seen) if v is not None]
        return views, len(seen) - len(views)

    async def target_label(self, event_id: str) -> str:
        """One line for an approval card: the target's times in IST and its title,
        labelled UNTRUSTED. Never raises; a failed fetch is 'target unverified'."""
        try:
            item = await self._call("GET", f"{_BASE}/{valid_event_id(event_id)}")
        except CalendarError:
            return "target unverified (the event could not be fetched)"
        if not isinstance(item, dict):
            return "target unverified (the event could not be read)"
        title = clean_external(item.get("summary"), 80).replace(chr(10), " ")
        return (
            f"UNTRUSTED event text from Google, shown for identification only: "
            f"title={title!r}; start={_when(item.get('start')) or '?'}; "
            f"end={_when(item.get('end')) or '?'}"
        )

    async def get_event(self, event_id: str) -> EventView:
        data = await self._call("GET", f"{_BASE}/{valid_event_id(event_id)}")
        view = to_view(data)
        if view is None:
            raise CalendarError("That calendar event was not found.", code="not_found")
        return view

    async def create_event(self, plan: EventPlan) -> bool:
        """True when created now, False when this exact event already exists
        (replay). A 409 whose event is `cancelled` (it was created and then
        deleted: Google keeps the id) is an error, not a success."""
        try:
            await self._call("POST", _BASE, params={"sendUpdates": "none"}, body=plan.body)
        except CalendarError as exc:
            if exc.code != "conflict":
                raise
            item = await self._call("GET", f"{_BASE}/{plan.event_id}")
            status = item.get("status") if isinstance(item, dict) else None
            if status == "cancelled":
                raise CalendarError(
                    "That event was created earlier and then deleted, so it cannot be re-created "
                    "by a replay. Ask for it again in a new message.",
                    code="cancelled",
                ) from None
            return False
        return True

    async def update_event(self, plan: EventPlan) -> None:
        await self._call(
            "PATCH", f"{_BASE}/{plan.event_id}", params={"sendUpdates": "none"}, body=plan.body
        )

    async def delete_event(self, event_id: str) -> bool:
        """True when deleted now, False when it was already gone."""
        try:
            await self._call(
                "DELETE", f"{_BASE}/{valid_event_id(event_id)}", params={"sendUpdates": "none"}
            )
        except CalendarError as exc:
            if exc.code in ("gone", "not_found"):
                return False
            raise
        return True
