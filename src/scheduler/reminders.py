# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Persisted reminders and their delivery (P3.3, R1, R11, ADR-0008).

* `ReminderStore`: every write that changes WHEN a reminder fires happens in one
  transaction together with its `reminder_fire` job (`JobQueue.enqueue_in`), so
  a reminder can never be stored without being scheduled, nor scheduled without
  being stored (the P2.5b "stored before scheduled" bug). Moving or deleting a
  reminder cancels the old job in the same transaction.
* `ReminderDelivery`: the job handler. It re-reads the reminder, skips it if it
  was deleted, already fired or moved (the job's `remind_at` no longer matches
  the row), and otherwise sends through the `Gateway` (the only chat is the
  allowlisted owner, R2), then marks it fired. Delivery is at-least-once: a crash
  between the send and the mark means one repeat on retry, never a loss.
* A reminder the user set explicitly always delivers: no quiet-hours or budget
  check here (H17 default; those belong to proactive messages, P5.1).
* Past the due time by more than `LATE_AFTER_S` the message says it is late and
  when it was due (catch-up after downtime).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.core.timeparse import format_ist
from src.gateway.protocols import Gateway
from src.memory.schemas.scheduling import Reminder
from src.scheduler.jobs import JobQueue
from src.scheduler.runner import JobContext

log = logging.getLogger(__name__)

KIND = "reminder_fire"
LATE_AFTER_S = 120.0


def _key(reminder_id: uuid.UUID) -> str:
    """One key per scheduling (create or move): a moved reminder gets a fresh job
    and the old one is cancelled, so an old key can never block the new one."""
    return f"reminder:{reminder_id}:{uuid.uuid4().hex[:12]}"


class ReminderStore:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], queue: JobQueue, clock: Clock
    ) -> None:
        self._sf = session_factory
        self._q = queue
        self._clock = clock

    async def _schedule(self, session: AsyncSession, r: Reminder) -> None:
        await self._q.enqueue_in(
            session,
            KIND,
            dedupe_key=_key(r.id),
            payload={"reminder_id": str(r.id), "remind_at": r.remind_at.isoformat()},
            run_at=r.remind_at,
            source_event_id=r.source_event_id,
        )

    async def create(
        self, title: str, remind_at: datetime, *, source_event_id: uuid.UUID | None = None
    ) -> Reminder:
        now = self._clock.now()
        async with self._sf() as session, session.begin():
            r = Reminder(
                id=uuid.uuid4(),
                title=title,
                remind_at=remind_at,
                status="pending",
                source_event_id=source_event_id,
                created_at=now,
                updated_at=now,
            )
            session.add(r)
            await session.flush()
            await self._schedule(session, r)  # same transaction: both or neither
        return r

    async def update(
        self, reminder_id: uuid.UUID, title: str, remind_at: datetime
    ) -> Reminder | None:
        now = self._clock.now()
        async with self._sf() as session, session.begin():
            r = await session.get(Reminder, reminder_id, with_for_update=True)
            if r is None:
                return None
            await self._q.cancel_by_payload_in(session, KIND, "reminder_id", str(r.id))
            r.title = title
            r.remind_at = remind_at
            r.status = "pending"
            r.fired_at = None
            r.updated_at = now
            await session.flush()
            await self._schedule(session, r)
        return r

    async def delete(self, reminder_id: uuid.UUID) -> bool:
        async with self._sf() as session, session.begin():
            await self._q.cancel_by_payload_in(session, KIND, "reminder_id", str(reminder_id))
            res = await session.execute(
                delete(Reminder).where(Reminder.id == reminder_id).returning(Reminder.id)
            )
            return res.scalar_one_or_none() is not None

    async def get(self, reminder_id: uuid.UUID) -> Reminder | None:
        async with self._sf() as session:
            return await session.get(Reminder, reminder_id)

    async def list(self, *, limit: int = 100) -> list[Reminder]:
        async with self._sf() as session:
            rows = await session.scalars(
                select(Reminder).order_by(Reminder.remind_at, Reminder.id).limit(limit)
            )
            return list(rows)


class ReminderDelivery:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: Gateway,
        clock: Clock,
    ) -> None:
        self._sf = session_factory
        self._gw = gateway
        self._clock = clock

    @staticmethod
    def _payload(ctx: JobContext) -> tuple[uuid.UUID, str]:
        try:
            return uuid.UUID(str(ctx.payload["reminder_id"])), str(ctx.payload["remind_at"])
        except (KeyError, ValueError):
            from src.scheduler.runner import PermanentJobError

            raise PermanentJobError("bad_payload") from None

    async def handle(self, ctx: JobContext) -> None:
        reminder_id, remind_at_iso = self._payload(ctx)
        async with self._sf() as session:
            r = await session.get(Reminder, reminder_id)
        if r is None or r.status != "pending" or r.remind_at.isoformat() != remind_at_iso:
            log.info("reminder skipped (deleted, fired or moved)")
            return
        now = self._clock.now()
        late = now - r.remind_at > timedelta(seconds=LATE_AFTER_S)
        text = f"Reminder: {r.title}"
        if late:
            text += f" (late: was due {format_ist(r.remind_at)})"
        await self._gw.send_text(text)
        async with self._sf() as session, session.begin():
            row = await session.get(Reminder, reminder_id, with_for_update=True)
            if row is not None and row.status == "pending" and row.remind_at == r.remind_at:
                row.status = "fired"
                row.fired_at = now
                row.updated_at = now

    async def on_dead(self, ctx: JobContext) -> None:
        reminder_id, _ = self._payload(ctx)
        async with self._sf() as session:
            r = await session.get(Reminder, reminder_id)
        if r is not None and r.status == "pending":
            await self._gw.send_text(f"I could not deliver your reminder: {r.title}")


def reminder_view(r: Reminder) -> dict[str, Any]:
    return {
        "reminder_id": str(r.id),
        "title": r.title,
        "remind_at": format_ist(r.remind_at),
        "remind_at_utc": r.remind_at.astimezone(UTC).isoformat(),
        "fired": r.status == "fired",
    }
