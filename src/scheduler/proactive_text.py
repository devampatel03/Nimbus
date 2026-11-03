# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Deterministic message templates for proactive jobs (P5.1). Zero model calls:
every string is assembled here from database rows.

Third-party or model-derived text (calendar titles, commitment descriptions,
task and reminder titles, memory previews) is DATA, never prose: it is
sanitised, clipped, and shown only inside curly quotes under a heading that says
where it came from. The fixed wording around it never changes. The gateway
still screens the whole message (link filter, canary) on the way out."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from src.core.timeparse import IST, format_ist, relative_phrase
from src.llm.scrub import redact_secrets

MAX_QUOTE = 80
MUTE_HINT = "Pause these with /snooze 2h or /quiet."

EVENING_QUESTIONS = (
    "What is the one thing you want done tomorrow?",
    "What is still on your mind from today?",
    "What went well today?",
    "Is anything blocking you right now?",
    "What can wait until next week?",
    "Who do you owe a reply?",
    "What would make tomorrow easy?",
)


def quote(raw: str, limit: int = MAX_QUOTE) -> str:
    """One-line, printable, secret-redacted, clipped, wrapped in curly quotes. The
    inner quote characters are removed so the data cannot close its own frame."""
    folded = unicodedata.normalize("NFKC", raw)
    folded = "".join(ch if ch.isprintable() else " " for ch in folded)
    for ch in '"“”‘’`':
        folded = folded.replace(ch, "'" if ch == '"' else "")
    folded = folded.replace("<", " ").replace(">", " ")  # no markup-shaped data
    text = " ".join(redact_secrets(folded).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return f"“{text}”"


def short_time(at: datetime) -> str:
    local = at.astimezone(IST)
    h12 = local.hour % 12 or 12
    return f"{h12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def day_label(day: date) -> str:
    return day.strftime("%a %d %b")


@dataclass(frozen=True)
class CalItem:
    title: str  # untrusted
    start_at: datetime | None
    all_day: bool = False


@dataclass(frozen=True)
class DueItem:
    description: str  # untrusted
    due_at: datetime


@dataclass(frozen=True)
class TimedItem:
    title: str  # untrusted
    at: datetime


@dataclass(frozen=True)
class BriefData:
    day: date
    now: datetime
    # None = Calendar not bound (section omitted); [] = bound and empty.
    calendar: list[CalItem] | None = None
    calendar_failed: bool = False
    due_today: list[DueItem] = field(default_factory=list)
    overdue: list[DueItem] = field(default_factory=list)
    open_tasks: list[str] = field(default_factory=list)
    open_tasks_total: int = 0
    reminders_today: list[TimedItem] = field(default_factory=list)
    candidates: int = 0

    def is_empty(self) -> bool:
        return not (
            self.calendar
            or self.calendar_failed
            or self.due_today
            or self.overdue
            or self.open_tasks_total
            or self.reminders_today
            or self.candidates
        )


def _more(shown: int, total: int) -> list[str]:
    return [f"  (+{total - shown} more)"] if total > shown else []


def morning(d: BriefData, *, items_max: int) -> str:
    lines = ["Good morning.", f"Your brief for {day_label(d.day)}."]
    if d.calendar is not None:
        lines.append("Calendar (titles come from your calendar, shown as quoted data):")
        if d.calendar:
            for e in d.calendar[:items_max]:
                when = "all day" if e.all_day else (short_time(e.start_at) if e.start_at else "?")
                lines.append(f"- {when} {quote(e.title)}")
            lines += _more(min(len(d.calendar), items_max), len(d.calendar))
        else:
            lines.append("- nothing scheduled")
    elif d.calendar_failed:
        lines.append("Calendar: could not be read this time.")
    if d.overdue:
        lines.append("Overdue commitments (quoted data):")
        for c in d.overdue[:items_max]:
            lines.append(f"- {quote(c.description)}, was due {format_ist(c.due_at)}")
        lines += _more(min(len(d.overdue), items_max), len(d.overdue))
    if d.due_today:
        lines.append("Commitments due today (quoted data):")
        for c in d.due_today[:items_max]:
            lines.append(f"- {quote(c.description)}, due {short_time(c.due_at)}")
        lines += _more(min(len(d.due_today), items_max), len(d.due_today))
    if d.reminders_today:
        lines.append("Your reminders today (quoted data):")
        for r in d.reminders_today[:items_max]:
            lines.append(f"- {short_time(r.at)} {quote(r.title)}")
        lines += _more(min(len(d.reminders_today), items_max), len(d.reminders_today))
    if d.open_tasks_total:
        lines.append(f"Open tasks: {d.open_tasks_total} (quoted data):")
        for t in d.open_tasks[:items_max]:
            lines.append(f"- {quote(t)}")
        lines += _more(min(len(d.open_tasks), items_max), d.open_tasks_total)
    if d.candidates:
        lines.append(f"{d.candidates} commitment candidate(s) are waiting for your confirmation.")
    lines.append(MUTE_HINT)
    return "\n".join(lines)


@dataclass(frozen=True)
class EveningData:
    day: date
    due_today: list[DueItem] = field(default_factory=list)
    overdue: list[DueItem] = field(default_factory=list)
    open_tasks_total: int = 0


def evening(d: EveningData) -> str:
    """Short, exactly one question."""
    lines = ["Evening check-in."]
    open_items = [*d.overdue, *d.due_today]
    if open_items:
        c = open_items[0]
        lines.append(
            f"Did you get to {quote(c.description)}? Tell me if it is done or the date moved."
        )
    else:
        counts = f"{d.open_tasks_total} open task(s). " if d.open_tasks_total else ""
        lines.append(counts + EVENING_QUESTIONS[d.day.weekday()])
    return "\n".join(lines)


@dataclass(frozen=True)
class WeeklyData:
    day: date
    open_commitments: int = 0
    due_next_7d: int = 0
    overdue: int = 0
    candidates: int = 0
    pending_memory: int = 0
    open_tasks: int = 0
    pending_reminders: int = 0
    cards_coming: int = 0


def weekly(d: WeeklyData) -> str:
    lines = [
        "Weekly review.",
        f"Week ending {day_label(d.day)}:",
        (
            f"- Commitments open: {d.open_commitments} ({d.due_next_7d} due in the next 7 days, "
            f"{d.overdue} overdue)"
        ),
        f"- Commitment candidates awaiting you: {d.candidates}",
        f"- Memory items awaiting your approval: {d.pending_memory}",
        f"- Open tasks: {d.open_tasks}",
        f"- Pending reminders: {d.pending_reminders}",
    ]
    if d.cards_coming:
        lines.append(
            f"I will send up to {d.cards_coming} memory item(s) next as approval cards; "
            "approve or reject each. Nothing is saved until you approve."
        )
    lines.append(MUTE_HINT)
    return "\n".join(lines)


def _ago(delta_s: int) -> str:
    if delta_s < 3600:
        return "just now" if delta_s < 60 else f"{delta_s // 60} min ago"
    if delta_s < 48 * 3600:
        return f"{delta_s // 3600} h ago"
    return f"{delta_s // 86400} days ago"


def nudge(description: str, due_at: datetime, now: datetime) -> str:
    if due_at > now:
        when = f"is due {format_ist(due_at)} ({relative_phrase(due_at, now)})"
    else:
        when = f"was due {format_ist(due_at)} ({_ago(int((now - due_at).total_seconds()))})"
    return f"Nudge: {quote(description)} {when}. Tell me if it is done or the date moved."
