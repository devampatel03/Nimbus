# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Pure time rules for proactive messages (P5.1): IST day, quiet hours, slots,
snooze durations. No I/O. India has no DST, so every IST wall time maps to one
instant and `+ timedelta(days=1)` is always 24 h."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from src.core.timeparse import IST

_DURATION = re.compile(r"^(\d{1,4})\s*(m|min|mins|h|hr|hrs|hour|hours|d|day|days)$", re.IGNORECASE)


def ist_date(now: datetime) -> date:
    return now.astimezone(IST).date()


def at_ist(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=IST)


def in_quiet(now: datetime, start: time, end: time) -> bool:
    """True inside [start, end) in IST, across midnight when start > end.
    start == end means no quiet hours."""
    if start == end:
        return False
    local = now.astimezone(IST).time().replace(tzinfo=None)
    if start < end:
        return start <= local < end
    return local >= start or local < end


def next_boundary(now: datetime, t: time) -> datetime:
    """The first instant strictly after `now` at which the IST clock reads `t`."""
    cand = at_ist(ist_date(now), t)
    return cand if cand > now else at_ist(ist_date(now) + timedelta(days=1), t)


def next_quiet_end(now: datetime, end: time) -> datetime:
    """When the quiet window `now` is in is over."""
    return next_boundary(now, end)


def night_mute_until(now: datetime, start: time, end: time) -> datetime:
    """`/quiet`: silence through the NEXT night: the first `end` after the next
    `start`."""
    return next_boundary(next_boundary(now, start), end)


def parse_duration(raw: str) -> timedelta | None:
    """'30m', '2h', '1d' -> timedelta. None for anything else or zero."""
    m = _DURATION.fullmatch(raw.strip())
    if m is None:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()[0]
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    return delta if delta > timedelta(0) else None
