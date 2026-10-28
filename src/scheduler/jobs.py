# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Durable job queue over the `job` table (ADR-0008, P1.2c schema).

* Claim: one statement, `UPDATE .. WHERE id IN (SELECT .. FOR UPDATE SKIP
  LOCKED)`, so concurrent runners never receive the same row. The claim commits
  at once; the handler runs OUTSIDE any transaction (a slow job holds no lock).
* Lease: a claimed row is `running` with `locked_at`. If the runner dies, the row
  becomes claimable again once `locked_at + lease_s` has passed (crash
  recovery). A finish from a worker that lost its lease is refused (`complete`
  and `fail` match on `locked_by` AND `locked_at`).
* Retry: a failure re-queues with exponential backoff; at `max_attempts` (or on
  a permanent failure, or a lease that expires at the last attempt) the job is
  `dead` (dead letter) and never runs again.
* Idempotency: `dedupe_key` is UNIQUE; enqueueing an existing key is a no-op.
  `enqueue_in` joins the caller's transaction, so a domain row and its job
  commit together or not at all.
* Cancel: `cancel` marks pending or running jobs `cancelled`; a late finish from
  an in-flight handler cannot resurrect it.

Delivery is at-least-once. A handler must be idempotent.

Time comes from an injected `Clock`; `locked_at`, `run_at` and backoff all use
it, never the database clock, so tests drive time with a fake clock. Stored
errors are the exception TYPE NAME only (INV-7: messages can carry user text).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, delete, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.memory.schemas.ops import Job

DEFAULT_LEASE_S = 300.0
DEFAULT_BACKOFF_BASE_S = 30.0
DEFAULT_BACKOFF_MAX_S = 3600.0
DEFAULT_MAX_ATTEMPTS = 5
INTERRUPTED = "Interrupted"
_ERROR_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def safe_error_code(value: str) -> str:
    """Identifier-shaped, bounded: safe to persist and log."""
    return _ERROR_RE.sub("?", value)[:100] or "Error"


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    kind: str
    dedupe_key: str
    payload: dict[str, Any]
    run_at: datetime
    attempts: int  # including this run
    max_attempts: int
    locked_by: str
    locked_at: datetime
    source_event_id: uuid.UUID | None
    proactive: bool


@dataclass(frozen=True)
class ClaimResult:
    jobs: list[ClaimedJob]
    dead: list[ClaimedJob]  # leases that expired at the last attempt: now dead-lettered


def _claimed(row: Any) -> ClaimedJob:
    return ClaimedJob(
        id=row.id,
        kind=row.kind,
        dedupe_key=row.dedupe_key,
        payload=dict(row.payload),
        run_at=row.run_at,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        locked_by=row.locked_by or "",
        locked_at=row.locked_at,
        source_event_id=row.source_event_id,
        proactive=row.proactive,
    )


class JobQueue:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        worker_id: str | None = None,
        lease_s: float = DEFAULT_LEASE_S,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
    ) -> None:
        if lease_s <= 0 or backoff_base_s <= 0 or backoff_max_s < backoff_base_s:
            raise ValueError("invalid queue timing")
        self._sf = session_factory
        self._clock = clock
        self.worker_id = worker_id or f"w-{uuid.uuid4().hex[:12]}"
        self.lease_s = lease_s
        self._base = backoff_base_s
        self._max = backoff_max_s

    # -- producing -----------------------------------------------------------

    async def enqueue_in(
        self,
        session: AsyncSession,
        kind: str,
        *,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
        run_at: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        source_event_id: uuid.UUID | None = None,
        proactive: bool = False,
        rearm: bool = False,
    ) -> tuple[uuid.UUID, bool]:
        """Insert in the caller's transaction (no commit). Returns `(id, created)`.
        An existing `dedupe_key` is left alone, unless `rearm` is set and the job
        is not currently running: then it is reset to pending at the new
        `run_at` with the new payload (used by the memory_extract sweeper to
        revive a job that died of a transient cause)."""
        now = self._clock.now()
        when = run_at or now
        if when.tzinfo is None:
            raise ValueError("run_at must be timezone-aware")
        values = {
            "kind": kind,
            "dedupe_key": dedupe_key,
            "payload": payload or {},
            "run_at": when,
            "max_attempts": max_attempts,
            "source_event_id": source_event_id,
            "proactive": proactive,
            "created_at": now,
            "updated_at": now,
        }
        stmt = pg_insert(Job).values(**values)
        if rearm:
            stmt = stmt.on_conflict_do_update(
                index_elements=[Job.dedupe_key],
                set_={
                    "run_at": when,
                    "payload": payload or {},
                    "status": "pending",
                    "attempts": 0,
                    "max_attempts": max_attempts,
                    "last_error": None,
                    "locked_by": None,
                    "locked_at": None,
                    "updated_at": now,
                },
                where=Job.status != "running",
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=[Job.dedupe_key])
        # xmax = 0 only on a freshly inserted row; a re-armed (updated) row has xmax != 0.
        got: Any = (
            await session.execute(
                stmt.returning(Job.id, literal_column("(xmax = 0)").label("inserted"))
            )
        ).one_or_none()
        if got is not None:
            return got.id, bool(got.inserted)
        found = await session.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key))
        assert found is not None
        return found, False

    async def enqueue(self, kind: str, *, dedupe_key: str, **kwargs: Any) -> tuple[uuid.UUID, bool]:
        async with self._sf() as session, session.begin():
            return await self.enqueue_in(session, kind, dedupe_key=dedupe_key, **kwargs)

    # -- consuming -----------------------------------------------------------

    async def claim(self, *, kinds: frozenset[str], limit: int) -> ClaimResult:
        """Lease up to `limit` due jobs of `kinds`: pending and due, or running with
        an expired lease. Expired leases already at `max_attempts` are dead-lettered
        instead of being run again."""
        if not kinds or limit < 1:
            return ClaimResult([], [])
        now = self._clock.now()
        expired = now - timedelta(seconds=self.lease_s)
        kind_list = sorted(kinds)
        async with self._sf() as session, session.begin():
            stale = (
                select(Job.id)
                .where(
                    Job.kind.in_(kind_list),
                    Job.status == "running",
                    Job.locked_at <= expired,
                    Job.attempts >= Job.max_attempts,
                )
                .with_for_update(skip_locked=True)
            )
            dead_rows = (
                await session.execute(
                    update(Job)
                    .where(Job.id.in_(stale))
                    .values(
                        status="dead",
                        last_error="LeaseExpired",
                        locked_by=None,
                        locked_at=None,
                        updated_at=now,
                    )
                    .returning(*Job.__table__.c)
                    .execution_options(synchronize_session=False)
                )
            ).all()
            due = (
                select(Job.id)
                .where(
                    Job.kind.in_(kind_list),
                    or_(
                        and_(Job.status == "pending", Job.run_at <= now),
                        and_(Job.status == "running", Job.locked_at <= expired),
                    ),
                )
                .order_by(Job.run_at, Job.created_at, Job.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            rows = (
                await session.execute(
                    update(Job)
                    .where(Job.id.in_(due))
                    .values(
                        status="running",
                        attempts=Job.attempts + 1,
                        locked_by=self.worker_id,
                        locked_at=now,
                        updated_at=now,
                    )
                    .returning(*Job.__table__.c)
                    .execution_options(synchronize_session=False)
                )
            ).all()
        jobs = sorted((_claimed(r) for r in rows), key=lambda j: (j.run_at, j.dedupe_key))
        return ClaimResult(jobs, [_claimed(r) for r in dead_rows])

    def _guard(self, job: ClaimedJob) -> Sequence[Any]:
        return (
            Job.id == job.id,
            Job.status == "running",
            Job.locked_by == job.locked_by,
            Job.locked_at == job.locked_at,
        )

    async def complete(self, job: ClaimedJob) -> bool:
        """True if this worker still held the lease and the job is now done."""
        async with self._sf() as session, session.begin():
            res = await session.execute(
                update(Job)
                .where(*self._guard(job))
                .values(
                    status="done",
                    last_error=None,
                    locked_by=None,
                    locked_at=None,
                    updated_at=self._clock.now(),
                )
                .returning(Job.id)
            )
            return res.scalar_one_or_none() is not None

    def backoff_s(self, attempts: int) -> float:
        return float(min(self._max, self._base * (2 ** max(0, attempts - 1))))

    async def fail(self, job: ClaimedJob, error: str, *, permanent: bool = False) -> str | None:
        """Re-queue with backoff, or dead-letter. Returns the new status, or None
        if the lease was lost (nothing changed)."""
        now = self._clock.now()
        dead = permanent or job.attempts >= job.max_attempts
        values: dict[str, Any] = {
            "status": "dead" if dead else "pending",
            "last_error": safe_error_code(error),
            "locked_by": None,
            "locked_at": None,
            "updated_at": now,
        }
        if not dead:
            values["run_at"] = now + timedelta(seconds=self.backoff_s(job.attempts))
        async with self._sf() as session, session.begin():
            res = await session.execute(
                update(Job).where(*self._guard(job)).values(**values).returning(Job.status)
            )
            status: str | None = res.scalar_one_or_none()
            return status

    async def release(self, job: ClaimedJob) -> bool:
        """Give the job back untouched (graceful shutdown): pending again at once,
        and the aborted run does not count as an attempt."""
        async with self._sf() as session, session.begin():
            res = await session.execute(
                update(Job)
                .where(*self._guard(job))
                .values(
                    status="pending",
                    attempts=Job.attempts - 1,
                    locked_by=None,
                    locked_at=None,
                    updated_at=self._clock.now(),
                )
                .returning(Job.id)
            )
            return res.scalar_one_or_none() is not None

    async def cancel(self, dedupe_key: str) -> bool:
        async with self._sf() as session, session.begin():
            return await self.cancel_in(session, dedupe_key)

    async def cancel_in(self, session: AsyncSession, dedupe_key: str) -> bool:
        res = await session.execute(
            update(Job)
            .where(Job.dedupe_key == dedupe_key, Job.status.in_(("pending", "running")))
            .values(
                status="cancelled", locked_by=None, locked_at=None, updated_at=self._clock.now()
            )
            .returning(Job.id)
        )
        return res.scalar_one_or_none() is not None

    async def cancel_by_payload_in(
        self, session: AsyncSession, kind: str, field: str, value: str
    ) -> int:
        """Cancel every live job of `kind` whose payload `field` equals `value`
        (caller's transaction). Returns how many were cancelled."""
        res = await session.execute(
            update(Job)
            .where(
                Job.kind == kind,
                Job.payload[field].astext == value,
                Job.status.in_(("pending", "running")),
            )
            .values(
                status="cancelled", locked_by=None, locked_at=None, updated_at=self._clock.now()
            )
            .returning(Job.id)
        )
        return len(res.all())

    async def defer(self, job: ClaimedJob, until: datetime) -> bool:
        """Put the job back to pending at `until` WITHOUT counting this run as an
        attempt (the work was not tried: the model is switched off or the daily
        budget is used). False if the lease was lost."""
        async with self._sf() as session, session.begin():
            res = await session.execute(
                update(Job)
                .where(*self._guard(job))
                .values(
                    status="pending",
                    attempts=Job.attempts - 1,
                    run_at=until,
                    locked_by=None,
                    locked_at=None,
                    updated_at=self._clock.now(),
                )
                .returning(Job.id)
            )
            return res.scalar_one_or_none() is not None

    async def recover_orphans(self) -> int:
        """Boot recovery for the single-process design: every `running` row at
        startup belongs to a process that is gone. Release them to pending at once
        (no waiting for the lease). The first interruption does not count as an
        attempt; a job interrupted again straight after (`last_error` still
        'Interrupted') does, so a job that kills the process cannot loop forever.
        Call once, before the runner loops start."""
        async with self._sf() as session, session.begin():
            res = await session.execute(
                update(Job)
                .where(Job.status == "running", Job.locked_by != self.worker_id)
                .values(
                    status="pending",
                    attempts=case(
                        (Job.last_error == INTERRUPTED, Job.attempts),
                        else_=func.greatest(Job.attempts - 1, 0),
                    ),
                    last_error=INTERRUPTED,
                    locked_by=None,
                    locked_at=None,
                    updated_at=self._clock.now(),
                )
                .returning(Job.id)
            )
            return len(res.all())

    async def purge_finished(self, *, older_than: timedelta) -> int:
        """Delete done / dead / cancelled jobs last touched before `now - older_than`.
        Keep `older_than` longer than any sweeper look-back that relies on the
        dedupe key of a finished job."""
        cutoff = self._clock.now() - older_than
        async with self._sf() as session, session.begin():
            res = await session.execute(
                delete(Job)
                .where(Job.status.in_(("done", "dead", "cancelled")), Job.updated_at < cutoff)
                .returning(Job.id)
            )
            return len(res.all())
