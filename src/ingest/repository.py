# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Event persistence via the P1.2c `event` ORM. Idempotent on `source_key`
(UNIQUE): `INSERT .. ON CONFLICT DO NOTHING`, so a redelivered update (or a
re-run follow-on) is a no-op that returns the existing id."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.contracts.event import Event, EventKind
from src.ingest.protocols import PendingFollowOn
from src.memory.schemas.event import Event as EventRow


class PgEventSink:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def record(self, event: Event) -> tuple[uuid.UUID, bool]:
        async with self._sf() as s, s.begin():
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
        async with self._sf() as s:
            found: uuid.UUID | None = await s.scalar(
                select(EventRow.id).where(EventRow.source_key == source_key)
            )
            return found

    async def pending_follow_ons(self, limit: int) -> list[PendingFollowOn]:
        child = EventRow.__table__.alias("child")
        async with self._sf() as s:
            q = (
                select(EventRow)
                .where(
                    EventRow.kind.in_([EventKind.VOICE.value, EventKind.RECORDER.value]),
                    ~select(child.c.id)
                    .where(child.c.source_key == EventRow.source_key + ":transcript")
                    .exists(),
                )
                .order_by(EventRow.created_at)
                .limit(limit)
            )
            return [
                PendingFollowOn(
                    id=r.id,
                    kind=EventKind(r.kind),
                    lane=r.lane,
                    meta=dict(r.meta),
                    file_unique_id=r.file_unique_id,
                )
                for r in (await s.execute(q)).scalars()
            ]
