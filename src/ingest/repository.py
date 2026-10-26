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

from sqlalchemy import Integer, Text, cast, func, select
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

    async def bump_attempts(self, event_id: uuid.UUID) -> int:
        key = f"{_ATTEMPTS_PREFIX}{event_id}"
        async with _fixed_errors(), self._sf() as s, s.begin():
            ins = pg_insert(SystemFlag).values(key=key, value={"n": 1}, updated_by="ingest")
            stmt = ins.on_conflict_do_update(
                index_elements=[SystemFlag.key],
                set_={
                    "value": func.jsonb_build_object(
                        "n", cast(SystemFlag.value["n"].astext, Integer) + 1
                    ),
                    "updated_at": func.now(),
                },
            ).returning(SystemFlag.value["n"].astext)
            return int((await s.execute(stmt)).scalar_one())

    async def pending_follow_ons(
        self, limit: int, *, older_than: datetime
    ) -> list[PendingFollowOn]:
        child = EventRow.__table__.alias("child")
        attempts = func.coalesce(cast(SystemFlag.value["n"].astext, Integer), 0)
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
