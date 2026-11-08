# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Proactive-message bookkeeping (P5.1, ADR-0011): the daily interruption budget,
per-key dedupe and the user's mute, in ONE `system_flag` row updated under a row
lock, so two runners cannot both spend the last slot.

`reserve` is called BEFORE the send and `release` undoes it if the send fails:
a crash between send and the end of the job loses a reservation at worst (one
message fewer), never repeats a message. Entries older than `KEEP_DAYS` are
pruned. The row holds keys and kinds only, never message text (INV-7).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.memory.schemas.ops import SystemFlag
from src.scheduler.proactive_rules import ist_date

STATE_KEY = "proactive_state"
UPDATED_BY = "proactive"
KEEP_DAYS = 8


class Reservation(enum.Enum):
    OK = "ok"
    DUPLICATE = "duplicate"  # this key was already sent
    OVER_BUDGET = "over_budget"  # the daily budget is spent
    OVER_KIND_CAP = "over_kind_cap"


@dataclass(frozen=True)
class Snapshot:
    sent_today: int
    muted_until: datetime | None
    held: int = 0  # untrusted-origin reminders dropped for budget since the last brief


def _sent(value: dict[str, Any], today: date) -> list[dict[str, str]]:
    keep_from = (today - timedelta(days=KEEP_DAYS)).isoformat()
    raw = value.get("sent")
    items = raw if isinstance(raw, list) else []
    return [
        {"key": str(i["key"]), "kind": str(i["kind"]), "day": str(i["day"])}
        for i in items
        if isinstance(i, dict) and {"key", "kind", "day"} <= i.keys() and str(i["day"]) >= keep_from
    ]


def _muted(value: dict[str, Any]) -> datetime | None:
    raw = value.get("muted_until")
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


class ProactiveState:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._sf = session_factory
        self._clock = clock

    async def _row(self, s: AsyncSession) -> SystemFlag:
        await s.execute(
            pg_insert(SystemFlag)
            .values(key=STATE_KEY, value={}, updated_by=UPDATED_BY)
            .on_conflict_do_nothing(index_elements=[SystemFlag.key])
        )
        return (
            await s.execute(select(SystemFlag).where(SystemFlag.key == STATE_KEY).with_for_update())
        ).scalar_one()

    async def snapshot(self) -> Snapshot:
        today = ist_date(self._clock.now())
        async with self._sf() as s, s.begin():
            value = dict((await self._row(s)).value)
        return Snapshot(
            sent_today=sum(1 for i in _sent(value, today) if i["day"] == today.isoformat()),
            muted_until=_muted(value),
            held=int(value.get("held", 0) or 0),
        )

    async def add_held(self, delta: int) -> None:
        """Count (delta > 0) or reset (delta == 0) the held-back reminders."""
        async with self._sf() as s, s.begin():
            row = await self._row(s)
            value = dict(row.value)
            value["held"] = 0 if delta == 0 else int(value.get("held", 0) or 0) + delta
            row.value = value

    async def sent_keys(self) -> set[str]:
        """Keys sent within the retention window (for cheap pre-checks)."""
        today = ist_date(self._clock.now())
        async with self._sf() as s, s.begin():
            value = dict((await self._row(s)).value)
        return {i["key"] for i in _sent(value, today)}

    async def reserve(
        self, key: str, kind: str, *, budget: int, kind_cap: int | None = None
    ) -> Reservation:
        """Atomically claim one slot of today's budget for `key`."""
        today = ist_date(self._clock.now())
        day = today.isoformat()
        async with self._sf() as s, s.begin():
            row = await self._row(s)
            value = dict(row.value)
            sent = _sent(value, today)
            if any(i["key"] == key for i in sent):
                return Reservation.DUPLICATE
            todays = [i for i in sent if i["day"] == day]
            if len(todays) >= budget:
                return Reservation.OVER_BUDGET
            if kind_cap is not None and sum(1 for i in todays if i["kind"] == kind) >= kind_cap:
                return Reservation.OVER_KIND_CAP
            sent.append({"key": key, "kind": kind, "day": day})
            value["sent"] = sent
            row.value = value
        return Reservation.OK

    async def release(self, key: str) -> None:
        today = ist_date(self._clock.now())
        async with self._sf() as s, s.begin():
            row = await self._row(s)
            value = dict(row.value)
            value["sent"] = [i for i in _sent(value, today) if i["key"] != key]
            row.value = value

    async def mute_until(self, until: datetime | None) -> None:
        """Set (or clear, with None) the user's mute."""
        if until is not None and until.tzinfo is None:
            raise ValueError("until must be timezone-aware")
        today = ist_date(self._clock.now())
        async with self._sf() as s, s.begin():
            row = await self._row(s)
            value = dict(row.value)
            value["sent"] = _sent(value, today)
            value["muted_until"] = until.isoformat() if until is not None else None
            row.value = value


class UntrustedReminderGate:
    """ADR-0011 addendum (P5.1): a reminder is exempt from the interruption budget
    only when a trusted user event created it. `reminders_create` is callable by
    the model, so a reminder created from injected text counts as a proactive
    message. `allow` reserves one budget slot; a repeat for the same reminder
    (at-least-once retry) is allowed because its slot is already held."""

    def __init__(self, state: ProactiveState, budget: int) -> None:
        self._state = state
        self._budget = budget

    async def allow(self, reminder_id: str) -> bool:
        got = await self._state.reserve(
            f"reminder:{reminder_id}", "reminder_untrusted", budget=self._budget
        )
        ok = got in (Reservation.OK, Reservation.DUPLICATE)
        if not ok:
            await self._state.add_held(1)
        return ok
