# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Proactive jobs (P5.1; R11, ADR-0011, ADR-0008): morning brief, evening
check-in, commitment nudges, weekly review.

* **Zero model calls.** Messages are templates built in code from database rows
  (`proactive_text`); nothing here imports an LLM client. An optional model "polish"
  was left out on purpose: a new prompt site needs an INV-6 allow-list entry in the
  protected eval suite (rule 4) and a human decision.
* **Scheduling.** `ProactiveScheduler.tick` (the scheduler loop) enqueues, for
  TODAY only, the scheduled slots whose time has come and are still worth
  sending (`*_valid_h`), under the dedupe key `proactive:<kind>:<IST day>`; a
  restart therefore re-enqueues nothing twice, and several missed slots collapse
  to the latest one (no burst). Nudges are scanned every `nudge_scan_s`.
* **Delivery** (`ProactiveHandler`, run by its own `JobRunner` so a slow brief
  cannot delay a reminder, R11): expired -> dropped; muted or in quiet hours ->
  deferred (`JobDeferred`, no attempt burned); then the budget is RESERVED
  (<= 6 a day across kinds, nudges capped and leaving room for the briefs),
  then sent through the gateway (link filter and canary apply). Over budget =
  dropped: commitments still due appear in the next brief, which is the fold.
* User-set reminders are not proactive and never pass through here (ADR-0011).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.core.timeparse import format_ist
from src.gateway.protocols import Gateway
from src.memory.commitments import Commitment, CommitmentLedgerStore
from src.memory.review import ReviewQueue
from src.memory.schemas.scheduling import Reminder, Task
from src.scheduler import proactive_text as tx
from src.scheduler.jobs import JobQueue
from src.scheduler.proactive_rules import (
    at_ist,
    in_quiet,
    ist_date,
    next_quiet_end,
    night_mute_until,
    parse_duration,
)
from src.scheduler.proactive_state import ProactiveState, Reservation
from src.scheduler.runner import JobContext, JobDeferred, JobRunner
from src.scheduler.settings import ProactiveSettings

log = logging.getLogger(__name__)

KIND_MORNING: Final = "proactive_morning"
KIND_EVENING: Final = "proactive_evening"
KIND_WEEKLY: Final = "proactive_weekly"
KIND_NUDGE: Final = "proactive_nudge"
PROACTIVE_KINDS: Final = frozenset({KIND_MORNING, KIND_EVENING, KIND_WEEKLY, KIND_NUDGE})
MAX_ATTEMPTS: Final = 3
MISSED_AFTER_S: Final = 300.0  # a slot enqueued later than this is a catch-up
CALENDAR_LIMIT: Final = 25


class CalendarSource(Protocol):
    async def between(self, start: datetime, end: datetime, limit: int) -> list[tx.CalItem]: ...


class CalendarClientSource:
    """Adapts the Google Calendar client; the only place a `CalendarClient` is touched."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def between(self, start: datetime, end: datetime, limit: int) -> list[tx.CalItem]:
        views, _ = await self._client.list_events(start, end, limit)
        return [tx.CalItem(v.title, v.start_at, v.all_day) for v in views]


def _due_key(i: tx.DueItem) -> datetime:
    return i.due_at


def slot_key(kind: str, day: date) -> str:
    return f"proactive:{kind}:{day.isoformat()}"


def nudge_key(commitment_key: uuid.UUID, day: date) -> str:
    return f"proactive:nudge:{commitment_key}:{day.isoformat()}"


# --------------------------------------------------------------------------
# data


class ProactiveData:
    """Read-only queries behind the templates."""

    def __init__(
        self,
        sf: async_sessionmaker[AsyncSession],
        ledger: CommitmentLedgerStore,
        settings: ProactiveSettings,
        *,
        review: ReviewQueue | None = None,
        calendar: CalendarSource | None = None,
    ) -> None:
        self._sf = sf
        self._ledger = ledger
        self._cfg = settings
        self._review = review
        self._calendar = calendar

    @staticmethod
    def _end_of_day(now: datetime) -> datetime:
        return at_ist(ist_date(now) + timedelta(days=1), datetime.min.time())

    async def _tasks(self, n: int) -> tuple[list[str], int]:
        async with self._sf() as s:
            total = int(
                await s.scalar(select(func.count()).select_from(Task).where(Task.done.is_(False)))
                or 0
            )
            rows = await s.scalars(
                select(Task.title).where(Task.done.is_(False)).order_by(Task.created_at).limit(n)
            )
            return list(rows), total

    async def _reminders(self, start: datetime, end: datetime, n: int) -> list[tx.TimedItem]:
        async with self._sf() as s:
            rows = await s.execute(
                select(Reminder.title, Reminder.remind_at)
                .where(
                    Reminder.status == "pending",
                    Reminder.remind_at >= start,
                    Reminder.remind_at < end,
                )
                .order_by(Reminder.remind_at)
                .limit(n)
            )
            return [tx.TimedItem(t, at) for t, at in rows.all()]

    async def pending_reminders(self) -> int:
        async with self._sf() as s:
            return int(
                await s.scalar(
                    select(func.count()).select_from(Reminder).where(Reminder.status == "pending")
                )
                or 0
            )

    async def _candidates(self) -> int:
        return len(await self._ledger.review_queue(limit=50))

    async def _split(
        self, now: datetime, before: datetime
    ) -> tuple[list[tx.DueItem], list[tx.DueItem]]:
        overdue: list[tx.DueItem] = []
        today: list[tx.DueItem] = []
        for c in await self._ledger.open_confirmed(due_before=before):
            if c.due_at is None:
                continue
            (overdue if c.due_at < now else today).append(tx.DueItem(c.description, c.due_at))
        return sorted(today, key=_due_key), sorted(overdue, key=_due_key)

    async def brief(self, now: datetime) -> tx.BriefData:
        day = ist_date(now)
        end = self._end_of_day(now)
        n = self._cfg.items_max
        calendar: list[tx.CalItem] | None = None
        failed = False
        if self._calendar is not None:
            try:
                start = at_ist(day, datetime.min.time())
                calendar = await self._calendar.between(start, end, CALENDAR_LIMIT)
            except Exception as exc:  # noqa: BLE001 - a calendar outage must not kill the brief
                log.warning("brief calendar failed err=%s", type(exc).__name__)
                failed = True
        due_today, overdue = await self._split(now, end)
        tasks, total = await self._tasks(n)
        return tx.BriefData(
            day=day,
            now=now,
            calendar=calendar,
            calendar_failed=failed,
            due_today=due_today,
            overdue=overdue,
            open_tasks=tasks,
            open_tasks_total=total,
            reminders_today=await self._reminders(now, end, n),
            candidates=await self._candidates(),
        )

    async def evening(self, now: datetime) -> tx.EveningData:
        due_today, overdue = await self._split(now, self._end_of_day(now))
        _, total = await self._tasks(1)
        return tx.EveningData(
            day=ist_date(now), due_today=due_today, overdue=overdue, open_tasks_total=total
        )

    async def weekly(self, now: datetime) -> tx.WeeklyData:
        week = now + timedelta(days=7)
        confirmed = await self._ledger.open_confirmed(limit=500)
        overdue = sum(1 for c in confirmed if c.due_at is not None and c.due_at < now)
        soon = sum(1 for c in confirmed if c.due_at is not None and now <= c.due_at <= week)
        pending = await self._review.pending_count() if self._review is not None else 0
        _, tasks = await self._tasks(1)
        return tx.WeeklyData(
            day=ist_date(now),
            open_commitments=len(confirmed),
            due_next_7d=soon,
            overdue=overdue,
            candidates=await self._candidates(),
            pending_memory=pending,
            open_tasks=tasks,
            pending_reminders=await self.pending_reminders(),
            cards_coming=min(pending, self._cfg.weekly_cards_max)
            if self._review is not None
            else 0,
        )

    async def nudge_candidates(self, now: datetime) -> list[Commitment]:
        """Confirmed commitments due within the lead time, or overdue by no more
        than `nudge_overdue_days_max` days. Oldest due first."""
        horizon = now + timedelta(hours=self._cfg.nudge_lead_h)
        floor = now - timedelta(days=self._cfg.nudge_overdue_days_max)
        out = [
            c
            for c in await self._ledger.open_confirmed(due_before=horizon)
            if c.due_at is not None and c.due_at >= floor
        ]
        return sorted(out, key=lambda c: c.due_at or now)


# --------------------------------------------------------------------------
# scheduling


@dataclass(frozen=True)
class _Slot:
    kind: str
    at: datetime
    valid_h: float

    @property
    def expires(self) -> datetime:
        return self.at + timedelta(hours=self.valid_h)


class ProactiveScheduler:
    """The scheduler-loop tick. Never raises (a failure is logged by type and
    retried on the next tick)."""

    def __init__(
        self,
        sf: async_sessionmaker[AsyncSession],
        queue: JobQueue,
        data: ProactiveData,
        state: ProactiveState,
        settings: ProactiveSettings,
        clock: Clock,
    ) -> None:
        self._sf = sf
        self._q = queue
        self._data = data
        self._state = state
        self._cfg = settings
        self._clock = clock
        self._enqueued: set[str] = set()
        self._skipped: set[str] = set()
        self._last_nudge_scan: datetime | None = None

    def _slots(self, now: datetime) -> list[_Slot]:
        c, t = self._cfg, self._cfg.times()
        day = ist_date(now)
        out: list[_Slot] = []
        if c.morning_enabled:
            out.append(_Slot(KIND_MORNING, at_ist(day, t["morning"]), c.morning_valid_h))
        if c.evening_enabled:
            out.append(_Slot(KIND_EVENING, at_ist(day, t["evening"]), c.evening_valid_h))
        if c.weekly_enabled and day.weekday() == c.weekly_weekday:
            out.append(_Slot(KIND_WEEKLY, at_ist(day, t["weekly"]), c.weekly_valid_h))
        return out

    async def tick(self) -> None:
        if not self._cfg.enabled or self._cfg.budget_per_day == 0:
            return
        try:
            now = self._clock.now()
            await self._schedule_slots(now)
            if self._cfg.nudges_enabled:
                await self._scan_nudges(now)
        except Exception as exc:  # noqa: BLE001 - the loop must keep ticking
            log.warning("proactive tick failed err=%s", type(exc).__name__)

    async def _schedule_slots(self, now: datetime) -> None:
        day = ist_date(now)
        live = [s for s in self._slots(now) if s.at <= now < s.expires]
        fresh = [s for s in live if (now - s.at).total_seconds() <= MISSED_AFTER_S]
        missed = sorted(
            (s for s in live if s not in fresh and slot_key(s.kind, day) not in self._skipped),
            key=lambda s: s.at,
        )
        # Several missed slots collapse to the latest one: a restart never bursts.
        for stale in missed[:-1]:
            self._skipped.add(slot_key(stale.kind, day))
        chosen = [*fresh, *missed[-1:]]
        for slot in chosen:
            key = slot_key(slot.kind, day)
            if key in self._enqueued:
                continue
            await self._enqueue(
                slot.kind,
                key,
                {"day": day.isoformat(), "expires_at": slot.expires.isoformat()},
                run_at=slot.at,
            )
            self._enqueued.add(key)

    async def _scan_nudges(self, now: datetime) -> None:
        last = self._last_nudge_scan
        if last is not None and (now - last).total_seconds() < self._cfg.nudge_scan_s:
            return
        self._last_nudge_scan = now
        day = ist_date(now)
        sent = await self._state.sent_keys()
        todays = sum(
            1 for k in sent if k.startswith("proactive:nudge:") and k.endswith(day.isoformat())
        )
        room = self._cfg.nudge_max_per_day - todays
        for c in await self._data.nudge_candidates(now):
            if room <= 0:
                break
            key = nudge_key(c.key, day)
            if key in self._enqueued or key in sent:
                continue
            expires = now + timedelta(hours=self._cfg.nudge_valid_h)
            await self._enqueue(
                KIND_NUDGE,
                key,
                {
                    "day": day.isoformat(),
                    "expires_at": expires.isoformat(),
                    "commitment_key": str(c.key),
                },
                run_at=now,
            )
            self._enqueued.add(key)
            room -= 1

    async def _enqueue(
        self, kind: str, key: str, payload: dict[str, Any], *, run_at: datetime
    ) -> None:
        async with self._sf() as s, s.begin():
            await self._q.enqueue_in(
                s,
                kind,
                dedupe_key=key,
                payload=payload,
                run_at=run_at,
                max_attempts=MAX_ATTEMPTS,
                proactive=True,
            )


# --------------------------------------------------------------------------
# delivery


class ProactiveHandler:
    def __init__(
        self,
        gateway: Gateway,
        data: ProactiveData,
        state: ProactiveState,
        ledger: CommitmentLedgerStore,
        settings: ProactiveSettings,
        clock: Clock,
        *,
        review: ReviewQueue | None = None,
    ) -> None:
        self._gw = gateway
        self._data = data
        self._state = state
        self._ledger = ledger
        self._cfg = settings
        self._clock = clock
        self._review = review

    def handlers(self) -> dict[str, Any]:
        return {k: self.handle for k in PROACTIVE_KINDS}

    async def handle(self, ctx: JobContext) -> None:
        cfg = self._cfg
        if not cfg.enabled or cfg.budget_per_day == 0:
            return
        now = ctx.now
        expires = _parse_iso(ctx.payload.get("expires_at"))
        if expires is None or now >= expires:
            log.info("proactive dropped: expired kind=%s", ctx.kind)
            return
        snap = await self._state.snapshot()
        if snap.muted_until is not None and now < snap.muted_until:
            self._defer_or_drop(ctx, snap.muted_until, expires)
            return
        t = cfg.times()
        if in_quiet(now, t["quiet_start"], t["quiet_end"]):
            self._defer_or_drop(ctx, next_quiet_end(now, t["quiet_end"]), expires)
            return

        built = await self._build(ctx, now)
        if built is None:
            return  # nothing worth saying
        text, after = built

        verdict = await self._state.reserve(
            ctx.dedupe_key,
            ctx.kind,
            budget=self._budget_for(ctx.kind),
            kind_cap=self._cap_for(ctx.kind),
        )
        if verdict is not Reservation.OK:
            log.info("proactive not sent kind=%s reason=%s", ctx.kind, verdict.value)
            return
        try:
            await self._gw.send_text(text)
        except BaseException:
            await self._state.release(ctx.dedupe_key)
            raise
        if after is not None:
            try:
                await after()
            except Exception as exc:  # noqa: BLE001 - the message itself was delivered
                log.warning(
                    "proactive follow-up failed kind=%s err=%s", ctx.kind, type(exc).__name__
                )

    def _budget_for(self, kind: str) -> int:
        c = self._cfg
        if kind != KIND_NUDGE:
            return c.budget_per_day
        reserved = int(c.morning_enabled) + int(c.evening_enabled)  # keep room for the briefs
        return max(0, c.budget_per_day - reserved)

    def _cap_for(self, kind: str) -> int | None:
        return self._cfg.nudge_max_per_day if kind == KIND_NUDGE else None

    @staticmethod
    def _defer_or_drop(ctx: JobContext, until: datetime, expires: datetime) -> None:
        if until >= expires:
            log.info("proactive dropped: muted past validity kind=%s", ctx.kind)
            return
        raise JobDeferred(until)

    async def _build(self, ctx: JobContext, now: datetime) -> tuple[str, Any] | None:
        cfg = self._cfg
        if ctx.kind == KIND_MORNING:
            brief = await self._data.brief(now)
            if cfg.skip_empty_brief and brief.is_empty():
                return None
            return tx.morning(brief, items_max=cfg.items_max), None
        if ctx.kind == KIND_EVENING:
            ev = await self._data.evening(now)
            return tx.evening(ev), None
        if ctx.kind == KIND_WEEKLY:
            wk = await self._data.weekly(now)
            return tx.weekly(wk), self._drain if wk.cards_coming else None
        if ctx.kind == KIND_NUDGE:
            return await self._build_nudge(ctx, now)
        log.error("proactive: unknown kind=%s", ctx.kind)
        return None

    async def _build_nudge(self, ctx: JobContext, now: datetime) -> tuple[str, Any] | None:
        try:
            key = uuid.UUID(str(ctx.payload["commitment_key"]))
        except (KeyError, ValueError):
            return None
        cur = await self._ledger.current(key)
        if cur is None or cur.status != "confirmed" or cur.due_at is None:
            return None  # completed, cancelled or moved to no date since it was queued
        return tx.nudge(cur.description, cur.due_at, now), None

    async def _drain(self) -> None:
        """Weekly review follow-up: up to N untrusted-memory approval cards. Cards
        are approval prompts (ADR-0011: not proactive messages); the review
        message itself was the one counted. Never approves anything."""
        if self._review is None:
            return
        sent = await self._review.drain(max_cards=self._cfg.weekly_cards_max)
        log.info("weekly review cards sent=%d", sent)


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


# --------------------------------------------------------------------------
# user commands (deterministic, no model)

_CMD = re.compile(r"^/(snooze|quiet|resume)(?:@\w{1,64})?(?:\s+(.{1,40}))?$", re.IGNORECASE)
SNOOZE_USAGE = "Usage: /snooze 30m, /snooze 2h or /snooze 1d. /quiet mutes through tonight. /resume turns them back on."


class ProactiveCommands:
    """`/snooze <duration>`, `/quiet`, `/resume`. Handled by code before the model
    sees the turn. Only proactive messages are muted; reminders you set still
    arrive (ADR-0011)."""

    def __init__(self, state: ProactiveState, settings: ProactiveSettings, clock: Clock) -> None:
        self._state = state
        self._cfg = settings
        self._clock = clock

    async def handle(self, text: str) -> str | None:
        m = _CMD.fullmatch(text.strip())
        if m is None:
            return None
        cmd, arg = m.group(1).lower(), (m.group(2) or "").strip()
        now = self._clock.now()
        t = self._cfg.times()
        if cmd == "resume":
            await self._state.mute_until(None)
            return "Proactive messages are back on."
        if cmd == "quiet":
            if arg:
                return SNOOZE_USAGE
            until = night_mute_until(now, t["quiet_start"], t["quiet_end"])
        else:
            delta = parse_duration(arg or "1h")
            if delta is None:
                return SNOOZE_USAGE
            limit = timedelta(hours=self._cfg.snooze_max_h)
            delta = min(delta, limit)
            until = now + delta
        await self._state.mute_until(until)
        return (
            f"Proactive messages are paused until {format_ist(until)}. "
            "Reminders you set still arrive. /resume turns them back on."
        )


# --------------------------------------------------------------------------
# assembly


@dataclass
class ProactiveParts:
    runner: JobRunner  # its own runner: a slow brief never queues behind a reminder (R11)
    scheduler: ProactiveScheduler
    commands: ProactiveCommands
    state: ProactiveState
    data: ProactiveData
    handler: ProactiveHandler


def build_proactive(
    sf: async_sessionmaker[AsyncSession],
    *,
    queue: JobQueue,
    gateway: Gateway,
    ledger: CommitmentLedgerStore,
    settings: ProactiveSettings,
    clock: Clock,
    review: ReviewQueue | None = None,
    calendar: CalendarSource | None = None,
    on_progress: Any = None,
) -> ProactiveParts:
    state = ProactiveState(sf, clock)
    data = ProactiveData(sf, ledger, settings, review=review, calendar=calendar)
    handler = ProactiveHandler(gateway, data, state, ledger, settings, clock, review=review)
    runner = JobRunner(
        queue,
        handler.handlers(),
        clock=clock,
        kinds=PROACTIVE_KINDS,
        batch=2,
        on_progress=on_progress,
    )
    return ProactiveParts(
        runner=runner,
        scheduler=ProactiveScheduler(sf, queue, data, state, settings, clock),
        commands=ProactiveCommands(state, settings, clock),
        state=state,
        data=data,
        handler=handler,
    )


class ChainScheduler:
    """Ticks several schedulers; one raising does not skip the others."""

    def __init__(self, *parts: Any) -> None:
        self._parts: Sequence[Any] = parts

    async def tick(self) -> None:
        for p in self._parts:
            try:
                await p.tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("scheduler tick failed err=%s", type(exc).__name__)
