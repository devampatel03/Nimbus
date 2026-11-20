# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Time resolution in Asia/Kolkata (ADR-0008, P3.3): storage is UTC, parsing and
rendering are IST. India has no DST, so every local time maps to exactly one
instant; the hard cases here are ambiguous wording, not clock changes.

`parse_when` turns a phrase or ISO-8601 string into a `ResolvedTime`. It never
guesses silently: whenever it filled a gap (no time given, day-first date, time
already passed today) the choice is recorded in `assumptions`, and
`confirmation_text` shows the resolved ABSOLUTE time back to the user.

Fail closed: anything unrecognised, ambiguous (a bare "5": AM or PM?), invalid
(31 Feb, 25:00), in the past, or more than five years ahead raises
`TimeParseError` with a stable `code` and a fixed message. Messages never echo
the input (it is model/user text; INV-7), only resolved dates.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)


def _rx(pattern: str) -> re.Pattern[str]:
    r"""ASCII-only classes: `\d` must not match other scripts' digits."""
    return re.compile(pattern, re.ASCII)


def _load_ist() -> tzinfo:
    try:
        return ZoneInfo("Asia/Kolkata")
    except ZoneInfoNotFoundError:  # no tzdata on the host (some Windows installs)
        log.warning("tzdata missing; using fixed +05:30 for Asia/Kolkata (India has no DST)")
        from datetime import timezone

        return timezone(timedelta(hours=5, minutes=30), "IST")


IST: tzinfo = _load_ist()

MAX_INPUT_CHARS = 200
MAX_HORIZON_YEARS = 5
DEFAULT_HOUR = 9  # used when the user gave a date but no time; always disclosed

_MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}  # fmt: skip
_WEEKDAY_RE = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_FULL_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FILLER = frozenset({"at", "on", "the", "of", "by", "around", "for", "@", "from", "this"})


class TimeParseError(ValueError):
    """`code`: empty | unrecognised | ambiguous | invalid_time | invalid_date | past | too_far."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedTime:
    at: datetime  # UTC instant (what is stored)
    local: datetime  # the same instant in IST (what is shown)
    assumptions: tuple[str, ...] = field(default=())


def _err(code: str, message: str) -> TimeParseError:
    return TimeParseError(code, message)


def parse_aware_iso(raw: object) -> datetime | None:
    """An ISO-8601 datetime with an explicit UTC offset ('Z' or 'z' = UTC), or
    None. No guessing: naive, over-long or malformed input is None."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 40:
        return None
    text = raw.strip()
    if text[-1] in "zZ":
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None and dt.utcoffset() is not None else None


def format_ist(dt: datetime) -> str:
    """'Wed 28 Oct 2026, 9:00 AM IST'."""
    local = dt.astimezone(IST)
    h12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return (
        f"{_DAY_NAMES[local.weekday()]} {local.day} {_MONTH_NAMES[local.month - 1]} "
        f"{local.year}, {h12}:{local.minute:02d} {ampm} IST"
    )


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def relative_phrase(at: datetime, now: datetime) -> str:
    secs = int((at - now).total_seconds())
    if secs < 60:
        return "in under a minute" if secs >= 0 else "overdue"
    if secs < 3600:
        return "in " + _plural(secs // 60, "minute")
    if secs < 48 * 3600:
        return "in " + _plural(secs // 3600, "hour")
    return "in " + _plural(secs // 86400, "day")


def confirmation_text(resolved: ResolvedTime, *, now: datetime) -> str:
    """The absolute time shown back to the user, plus any assumption made."""
    base = f"{format_ist(resolved.at)} ({relative_phrase(resolved.at, now)})"
    if resolved.assumptions:
        base += " - " + "; ".join(resolved.assumptions)
    return base


# -- helpers ----------------------------------------------------------------


def _add_months(d: date, months: int) -> tuple[date, bool]:
    idx = d.year * 12 + (d.month - 1) + months
    year, month = divmod(idx, 12)
    month += 1
    if year > 9998:
        raise _err("too_far", "That is too far ahead (limit is 5 years).")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last)), d.day > last


def _valid_date(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError:
        raise _err("invalid_date", "That date does not exist on the calendar.") from None


def _local(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def _horizon(now: datetime) -> datetime:
    local = now.astimezone(IST)
    d, _ = _add_months(local.date(), 12 * MAX_HORIZON_YEARS)
    return _local(d, local.time())


def _finish(local: datetime, now: datetime, assumptions: list[str]) -> ResolvedTime:
    at = local.astimezone(UTC)
    if at <= now:
        raise _err("past", f"That time has already passed ({format_ist(at)}).")
    if at > _horizon(now):
        raise _err("too_far", f"That is too far ahead ({format_ist(at)}; limit is 5 years).")
    return ResolvedTime(at=at, local=at.astimezone(IST), assumptions=tuple(assumptions))


def _hm(hour: int, minute: int) -> str:
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


# -- ISO 8601 ---------------------------------------------------------------

_ISO_RE = _rx(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"(z|[+-]\d{2}:?\d{2})?)?$"
)


def _parse_iso(s: str, now: datetime) -> ResolvedTime | None:
    m = _ISO_RE.match(s)
    if m is None:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    day = _valid_date(y, mo, d)
    assumptions: list[str] = []
    if m[4] is None:
        hour, minute, second = DEFAULT_HOUR, 0, 0
        assumptions.append(f"No time given, used {_hm(DEFAULT_HOUR, 0)}.")
    else:
        hour, minute, second = int(m[4]), int(m[5]), int(m[6] or 0)
        if hour > 23 or minute > 59 or second > 59:
            raise _err("invalid_time", "That clock time is not valid.")
    t = time(hour, minute, second)
    tz = m[7]
    if tz is None:
        return _finish(_local(day, t), now, assumptions)
    if tz == "z":
        offset = timedelta(0)
    else:
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        oh, om = int(digits[:2]), int(digits[2:])
        if oh > 23 or om > 59:
            raise _err("invalid_time", "That UTC offset is not valid.")
        offset = sign * timedelta(hours=oh, minutes=om)
    from datetime import timezone

    aware = datetime.combine(day, t, tzinfo=timezone(offset))
    return _finish(aware.astimezone(IST), now, assumptions)


# -- relative ("in 2 hours") ------------------------------------------------

_REL_RE = _rx(
    r"^in (?:(\d{1,30})|an?|half an?) ?(minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)$"
)
_UNIT_SECONDS = {"min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}


def _parse_relative(s: str, now: datetime) -> ResolvedTime | None:
    m = _REL_RE.match(s)
    if m is None:
        return None
    half = s.startswith("in half")
    n = int(m[1]) if m[1] is not None else 1
    unit = m[2].rstrip("s")
    if unit.startswith("min"):
        unit = "min"
    if half and unit not in ("hour", "hr"):
        return None
    local_now = now.astimezone(IST)
    assumptions: list[str] = []
    if unit in ("month", "year"):
        months = n * (12 if unit == "year" else 1)
        if months > 12 * (MAX_HORIZON_YEARS + 1):
            raise _err("too_far", "That is too far ahead (limit is 5 years).")
        d, clamped = _add_months(local_now.date(), months)
        if clamped:
            assumptions.append(
                f"The month is shorter, so used {d.day} {_MONTH_NAMES[d.month - 1]}."
            )
        return _finish(_local(d, local_now.time()), now, assumptions)
    seconds = 1800 if half else _UNIT_SECONDS[unit] * n
    if seconds > 400 * 86400 * 6:  # far beyond the horizon: avoid datetime overflow
        raise _err("too_far", "That is too far ahead (limit is 5 years).")
    return _finish((now + timedelta(seconds=seconds)).astimezone(IST), now, assumptions)


# -- compositional ("tomorrow at 9am", "next monday 10:30", "28 Oct 5pm") ----

_ORD = r"(?:st|nd|rd|th)?"
_RE_DAYWORD = _rx(r"\b(day after tomorrow|tomorrow|today|tonight|yesterday)\b")
_RE_WEEKDAY = _rx(rf"\b(?:(next|this)\s+)?({_WEEKDAY_RE})\b")
_RE_ISO_DATE = _rx(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RE_NUM_DATE = _rx(
    r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{4}|\d{2}))?\b(?!\s*[ap]\.?m\b)"  # 9.30pm is a time
)
_RE_D_MON = _rx(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH_RE})\b(?:\s+(\d{{4}})\b)?")
_RE_MON_D = _rx(rf"\b({_MONTH_RE})\s+(\d{{1,2}}){_ORD}\b(?:,?\s+(\d{{4}})\b)?")
_RE_NOON = _rx(r"\b(noon|midnight)\b")
_RE_MERIDIEM = _rx(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])")
_RE_COLON = _rx(r"\b(\d{1,2}):(\d{2})\b")
_RE_AT_HOUR = _rx(r"\bat\s+(\d{1,2})\b")


@dataclass
class _Date:
    kind: str  # 'offset' | 'weekday' | 'ymd' | 'md'
    offset: int = 0
    weekday: int = 0
    nxt: bool = False
    year: int | None = None
    month: int = 0
    day: int = 0
    tonight: bool = False
    day_first_note: str | None = None


def _take(pattern: re.Pattern[str], s: str) -> tuple[list[re.Match[str]], str]:
    found = list(pattern.finditer(s))
    return found, pattern.sub(" ", s)


def _extract_date(s: str) -> tuple[_Date | None, str]:
    found: list[_Date] = []
    ms, s = _take(_RE_DAYWORD, s)
    for m in ms:
        w = m[1]
        found.append(
            _Date(
                "offset",
                offset={"day after tomorrow": 2, "tomorrow": 1, "yesterday": -1}.get(w, 0),
                tonight=w == "tonight",
            )
        )
    ms, s = _take(_RE_ISO_DATE, s)
    for m in ms:
        found.append(_Date("ymd", year=int(m[1]), month=int(m[2]), day=int(m[3])))
    ms, s = _take(_RE_D_MON, s)
    for m in ms:
        found.append(
            _Date("ymd" if m[3] else "md", year=int(m[3]) if m[3] else None,
                  month=_MONTHS[m[2]], day=int(m[1]))
        )  # fmt: skip
    ms, s = _take(_RE_MON_D, s)
    for m in ms:
        found.append(
            _Date("ymd" if m[3] else "md", year=int(m[3]) if m[3] else None,
                  month=_MONTHS[m[1]], day=int(m[2]))
        )  # fmt: skip
    ms, s = _take(_RE_NUM_DATE, s)
    for m in ms:
        year = None if m[3] is None else int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        found.append(
            _Date("ymd" if year else "md", year=year, month=int(m[2]), day=int(m[1]),
                  day_first_note="day first")
        )  # fmt: skip
    ms, s = _take(_RE_WEEKDAY, s)
    for m in ms:
        found.append(_Date("weekday", weekday=_WEEKDAYS[m[2]], nxt=m[1] == "next"))
    if len(found) > 1:
        raise _err("unrecognised", "I could not read that time. Try e.g. 'tomorrow 9am'.")
    return (found[0] if found else None), s


def _ambiguous(hour: int, minute: int | None) -> TimeParseError:
    label = f"{hour}" if minute is None else f"{hour}:{minute:02d}"
    return _err("ambiguous", f"Did you mean {label} AM or {label} PM? Please say which.")


def _extract_time(s: str, *, pm_hint: bool) -> tuple[time | None, str]:
    found: list[time] = []
    for m in _RE_NOON.finditer(s):
        found.append(time(12, 0) if m[1] == "noon" else time(0, 0))
    s = _RE_NOON.sub(" ", s)
    for m in _RE_MERIDIEM.finditer(s):
        hour, minute = int(m[1]), int(m[2] or 0)
        if not 1 <= hour <= 12 or minute > 59:
            raise _err("invalid_time", "That clock time is not valid.")
        hour = hour % 12 + (12 if m[3].startswith("p") else 0)
        found.append(time(hour, minute))
    s = _RE_MERIDIEM.sub(" ", s)
    for m in _RE_COLON.finditer(s):
        hour, minute = int(m[1]), int(m[2])
        if hour > 23 or minute > 59:
            raise _err("invalid_time", "That clock time is not valid.")
        twenty_four = hour == 0 or hour >= 13 or len(m[1]) == 2 and m[1].startswith("0")
        if not twenty_four:
            if pm_hint:
                hour = hour % 12 + 12
            else:
                raise _ambiguous(hour, minute)
        found.append(time(hour, minute))
    s = _RE_COLON.sub(" ", s)
    for m in _RE_AT_HOUR.finditer(s):
        hour = int(m[1])
        if hour > 23:
            raise _err("invalid_time", "That clock time is not valid.")
        if hour == 0 or hour >= 13:
            found.append(time(hour, 0))
        elif pm_hint:
            found.append(time(hour % 12 + 12, 0))
        else:
            raise _ambiguous(hour, None)
    s = _RE_AT_HOUR.sub(" ", s)
    if len(found) > 1:
        raise _err("unrecognised", "I could not read that time. Try e.g. 'tomorrow 9am'.")
    return (found[0] if found else None), s


def _resolve_date(spec: _Date, t: time, local_now: datetime, assumptions: list[str]) -> date:
    today = local_now.date()
    if spec.kind == "offset":
        return today + timedelta(days=spec.offset)
    if spec.kind == "weekday":
        delta = (spec.weekday - today.weekday()) % 7
        cand = today + timedelta(days=delta)
        if cand == today and (spec.nxt or _local(cand, t) <= local_now):
            cand += timedelta(days=7)
            if not spec.nxt:
                assumptions.append("That time has passed today, so used next week.")
        elif spec.nxt:
            name = _FULL_DAYS[spec.weekday]
            assumptions.append(
                f"'next {name}' read as the coming {name} ({cand.day} "
                f"{_MONTH_NAMES[cand.month - 1]})."
            )
        return cand
    if spec.kind == "ymd":
        assert spec.year is not None
        return _valid_date(spec.year, spec.month, spec.day)
    # month/day without a year: the next date that exists and is not before today
    if not 1 <= spec.month <= 12 or not 1 <= spec.day <= 31:
        raise _err("invalid_date", "That date does not exist on the calendar.")
    for year in range(today.year, today.year + 9):  # 8 years always contains a leap day
        try:
            cand = date(year, spec.month, spec.day)
        except ValueError:
            continue
        if cand >= today:
            if year != today.year:
                assumptions.append(f"No year given, used {year}.")
            return cand
    raise _err("invalid_date", "That date does not exist on the calendar.")


def _parse_compositional(s: str, now: datetime) -> ResolvedTime:
    spec, rest = _extract_date(s)
    tonight = spec is not None and spec.tonight
    t, rest = _extract_time(rest, pm_hint=tonight)
    leftover = [w for w in rest.replace(",", " ").split() if w not in _FILLER]
    if leftover or (spec is None and t is None):
        raise _err("unrecognised", "I could not read that time. Try e.g. 'tomorrow 9am'.")
    local_now = now.astimezone(IST)
    assumptions: list[str] = []
    if "midnight" in s:
        assumptions.append("Midnight read as the start of that day (12:00 AM).")
    if t is None:
        default = time(20, 0) if tonight else time(DEFAULT_HOUR, 0)
        t = default
        assumptions.append(f"No time given, used {_hm(t.hour, t.minute)}.")
    if spec is None:
        day = local_now.date()
        if _local(day, t) <= local_now:
            day += timedelta(days=1)
            assumptions.append("That time has passed today, so used tomorrow.")
    else:
        day = _resolve_date(spec, t, local_now, assumptions)
        if spec.day_first_note:
            assumptions.append(
                f"Read the numeric date as day first: {day.day} {_MONTH_NAMES[day.month - 1]} "
                f"{day.year}."
            )
    return _finish(_local(day, t), now, assumptions)


def parse_when(text: str, *, now: datetime) -> ResolvedTime:
    """Resolve `text` (relative to `now`, an aware datetime) to a UTC instant."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(text, str) or not text.strip():
        raise _err("empty", "Say when, for example 'tomorrow 9am' or '2026-11-02 18:30'.")
    if len(text) > MAX_INPUT_CHARS:
        raise _err("unrecognised", "I could not read that time. Try e.g. 'tomorrow 9am'.")
    s = " ".join(text.lower().replace(" ", " ").split())
    resolved = _parse_iso(s, now) or _parse_relative(s, now)
    if resolved is not None:
        return resolved
    return _parse_compositional(s, now)
