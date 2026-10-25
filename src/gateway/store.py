# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Postgres persistence for the poll offset and inbound updates, using the
P1.2c tables `inbound_update` (UNIQUE update_id) and `system_flag`
(`telegram.offset`). No new table is needed.

Why the offset lags: `inbound_update` stores no payload, so once Telegram is
told (via the `getUpdates` offset) that an update is confirmed it cannot be
re-fetched. The offset therefore only moves past an update in the *same
transaction* that marks it processed (`complete`), never when it is merely
received. A crash between delivery and ack leaves the offset behind, Telegram
resends the update, and `begin()` reports it as `REDELIVERY`; one already
processed is `DONE` and skipped. That gives no loss and no duplicate effect
(R1) without storing message bodies.

The offset is `min(unprocessed update_id)` if any row is unprocessed, else
`max(update_id) + 1`, and never decreases.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.schemas.ops import InboundUpdate, SystemFlag

OFFSET_KEY = "telegram.offset"
_UPDATED_BY = "gateway"


class Seen(enum.Enum):
    NEW = "new"
    REDELIVERY = "redelivery"
    DONE = "done"


class UpdateStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get_offset(self) -> int | None:
        async with self._sf() as s:
            row = await s.get(SystemFlag, OFFSET_KEY)
            if row is None:
                return None
            v = row.value.get("offset")
            return v if isinstance(v, int) and not isinstance(v, bool) else None

    async def begin(self, update_id: int, from_user_id: int | None, kind: str) -> Seen:
        """Record receipt of a deliverable update (idempotent)."""
        async with self._sf() as s, s.begin():
            stmt = (
                pg_insert(InboundUpdate)
                .values(update_id=update_id, from_user_id=from_user_id, kind=kind)
                .on_conflict_do_nothing(index_elements=[InboundUpdate.update_id])
                .returning(InboundUpdate.id)
            )
            if (await s.execute(stmt)).first() is not None:
                return Seen.NEW
            done = await s.scalar(
                select(InboundUpdate.processed_at).where(InboundUpdate.update_id == update_id)
            )
            return Seen.DONE if done is not None else Seen.REDELIVERY

    async def complete(self, update_id: int, *, event_id: uuid.UUID | None = None) -> None:
        """Mark processed and advance the offset, atomically."""
        async with self._sf() as s, s.begin():
            vals: dict[str, Any] = {"processed_at": func.now()}
            if event_id is not None:
                vals["event_id"] = event_id
            await s.execute(
                update(InboundUpdate)
                .where(InboundUpdate.update_id == update_id, InboundUpdate.processed_at.is_(None))
                .values(**vals)
            )
            await self._advance(s)

    async def record_dropped(self, update_id: int, from_user_id: int | None, kind: str) -> None:
        """Audit row for an update that is not delivered (stranger, unsupported,
        malformed), inserted already-processed, with the offset advanced in the
        same transaction. Idempotent."""
        async with self._sf() as s, s.begin():
            await s.execute(
                pg_insert(InboundUpdate)
                .values(
                    update_id=update_id,
                    from_user_id=from_user_id,
                    kind=kind,
                    processed_at=func.now(),
                )
                .on_conflict_do_nothing(index_elements=[InboundUpdate.update_id])
            )
            await self._advance(s)

    async def _advance(self, s: AsyncSession) -> None:
        # Serialise concurrent advancers on the flag row so the max() below
        # cannot be computed from a stale snapshot.
        await s.execute(
            pg_insert(SystemFlag)
            .values(key=OFFSET_KEY, value={"offset": 0}, updated_by=_UPDATED_BY)
            .on_conflict_do_nothing(index_elements=[SystemFlag.key])
        )
        row = (
            await s.execute(
                select(SystemFlag).where(SystemFlag.key == OFFSET_KEY).with_for_update()
            )
        ).scalar_one()
        current = row.value.get("offset", 0)
        lowest_open = await s.scalar(
            select(func.min(InboundUpdate.update_id)).where(InboundUpdate.processed_at.is_(None))
        )
        if lowest_open is not None:
            target = lowest_open
        else:
            highest = await s.scalar(select(func.max(InboundUpdate.update_id)))
            target = (highest + 1) if highest is not None else 0
        new = max(int(current), int(target))
        if new != current:
            row.value = {"offset": new}
            row.updated_by = _UPDATED_BY
            await s.flush()
