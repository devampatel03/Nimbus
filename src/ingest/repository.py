# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Event persistence via the P1.2c `event` ORM. Idempotent on `source_key`
(UNIQUE): `INSERT .. ON CONFLICT DO NOTHING`, so a redelivered update (or a
re-run follow-on) is a no-op that returns the existing id.

Every database failure is mapped to `EventStoreError("event_store_failed")`
raised `from None`: SQLAlchemy errors embed the statement and its bound
parameters (user text), which must never reach logs (INV-7).

Follow-on attempt counters live in `system_flag` under
`ingest.attempts:<event_id>` (no schema change).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import (
    ColumnElement,
    DateTime,
    Float,
    Integer,
    Text,
    case,
    cast,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.contracts.event import Event, EventKind
from src.ingest.protocols import EventStoreError, PendingFollowOn
from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.ops import SystemFlag

_ATTEMPTS_PREFIX = "ingest.attempts:"


@asynccontextmanager
async def _fixed_errors() -> AsyncIterator[None]:
    try:
        yield
    except SQLAlchemyError:
        raise EventStoreError("event_store_failed") from None


class PgEventSink:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def record(self, event: Event) -> tuple[uuid.UUID, bool]:
        async with _fixed_errors(), self._sf() as s, s.begin():
            stmt = (
                pg_insert(EventRow)
                .values(
                    source_key=event.source_key,
                    kind=event.kind.value,
                    source=event.source,
                    lane=event.lane,
                    confidence=event.confidence,
                    body=event.body,
                    meta=event.meta,
                    file_unique_id=event.file_unique_id,
                    occurred_at=event.occurred_at,
                )
                .on_conflict_do_nothing(index_elements=[EventRow.source_key])
                .returning(EventRow.id)
            )
            new_id = (await s.execute(stmt)).scalar_one_or_none()
            if new_id is not None:
                return new_id, True
            existing = await s.scalar(
                select(EventRow.id).where(EventRow.source_key == event.source_key)
            )
            assert existing is not None  # conflict implies the row exists
            return existing, False

    async def get_id(self, source_key: str) -> uuid.UUID | None:
        async with _fixed_errors(), self._sf() as s:
            found: uuid.UUID | None = await s.scalar(
                select(EventRow.id).where(EventRow.source_key == source_key)
            )
            return found

    async def bump_attempts(
        self, event_id: uuid.UUID, *, now: datetime, backoff_s: tuple[float, ...]
    ) -> int | None:
        key = f"{_ATTEMPTS_PREFIX}{event_id}"
        stamp = now.isoformat()
        async with _fixed_errors(), self._sf() as s, s.begin():
            ins = pg_insert(SystemFlag).values(
                key=key, value={"n": 1, "at": stamp}, updated_by="ingest"
            )
            stmt = ins.on_conflict_do_update(
                index_elements=[SystemFlag.key],
                set_={
                    "value": func.jsonb_build_object("n", _n_expr() + 1, "at", stamp),
                    "updated_at": func.now(),
                },
                # Atomic claim: only when the backoff since the last attempt elapsed.
                where=_due_expr(now, backoff_s),
            ).returning(SystemFlag.value["n"].astext)
            got = (await s.execute(stmt)).scalar_one_or_none()
            return None if got is None else int(got)

    async def clear_attempts(self, event_id: uuid.UUID) -> None:
        async with _fixed_errors(), self._sf() as s, s.begin():
            await s.execute(
                delete(SystemFlag).where(SystemFlag.key == f"{_ATTEMPTS_PREFIX}{event_id}")
            )

    async def pending_follow_ons(
        self, limit: int, *, older_than: datetime, now: datetime, backoff_s: tuple[float, ...]
    ) -> list[PendingFollowOn]:
        child = EventRow.__table__.alias("child")
        attempts = func.coalesce(_n_expr(), 0)
        async with _fixed_errors(), self._sf() as s:
            q = (
                select(EventRow, attempts.label("attempts"))
                .outerjoin(
                    SystemFlag,
                    SystemFlag.key == func.concat(_ATTEMPTS_PREFIX, cast(EventRow.id, Text)),
                )
                .where(
                    EventRow.kind.in_([EventKind.VOICE.value, EventKind.RECORDER.value]),
                    EventRow.occurred_at <= older_than,
                    EventRow.file_unique_id.is_not(None),
                    EventRow.meta["file_id"].astext.is_not(None),
                    EventRow.meta["update_id"].astext.is_not(None),
                    ~select(child.c.id)
                    .where(child.c.source_key == EventRow.source_key + ":transcript")
                    .exists(),
                    or_(SystemFlag.key.is_(None), _due_expr(now, backoff_s)),
                )
                .order_by(attempts, EventRow.occurred_at)
                .limit(limit)
            )
            return [
                PendingFollowOn(
                    id=r.id,
                    kind=EventKind(r.kind),
                    lane=r.lane,
                    meta=dict(r.meta),
                    file_unique_id=r.file_unique_id,
                    occurred_at=r.occurred_at,
                    attempts=int(n),
                )
                for r, n in (await s.execute(q)).all()
            ]


def _n_expr() -> ColumnElement[int]:
    return cast(SystemFlag.value["n"].astext, Integer)


def _due_expr(now: datetime, backoff_s: tuple[float, ...]) -> ColumnElement[bool]:
    """Backoff elapsed since the last recorded attempt (columns of `system_flag`)."""
    gaps = {i + 1: g for i, g in enumerate(backoff_s)}
    gap = case(gaps, value=_n_expr(), else_=backoff_s[-1])
    last = cast(SystemFlag.value["at"].astext, DateTime(timezone=True))
    return last + func.make_interval(0, 0, 0, 0, 0, 0, cast(gap, Float)) <= now
