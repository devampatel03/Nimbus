# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Postgres persistence for the poll offset and inbound updates, using the
P1.2c tables `inbound_update` (UNIQUE update_id) and `system_flag`
(`telegram.offset`, `telegram.redeliveries`). No new table is needed
(ADR-0016).

Why the offset lags: `inbound_update` stores no payload, so once Telegram is
told (via the `getUpdates` offset) that an update is confirmed it cannot be
re-fetched. The offset therefore only moves past an update in the *same
transaction* that marks it processed (`complete`), never when it is merely
received. A crash between delivery and ack leaves the offset behind, Telegram
resends the update, and `begin()` reports it as `REDELIVERY`; one already
processed is `DONE` and skipped.

Delivery is AT LEAST ONCE: a crash after the consumer stored its event but
before `ack` redelivers the update. The consumer must therefore be idempotent
on `event.source_key = "telegram:<update_id>"` (P2.2 acceptance). This module
does not by itself guarantee "no duplicate effect".

Poison updates: every redelivery increments a persisted per-update counter
(`telegram.redeliveries`, survives crash loops). Past `max_deliveries` AND once
the update is at least `poison_min_age_s` old (since `received_at`, default 10
minutes: a short consumer outage must not turn a legitimate message into a
poison) the update is marked processed (`processed_at` set, no event) and the
offset moves on, so one un-ackable update cannot pin the offset or starve the
queue. Poisoning also records a pending user notice in the same transaction
(`telegram.poison_notices`: update_id -> received_at); the gateway sends the
fixed "could not process a message at HH:MM, please resend" text and then
clears it, so a crash or a failed send cannot lose the notice. No message
content is ever stored in it.

The offset is `min(unprocessed update_id)` if any row is unprocessed, else
`max(update_id) + 1`, and never decreases.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.schemas.ops import InboundUpdate, SystemFlag

OFFSET_KEY = "telegram.offset"
REDELIVERY_KEY = "telegram.redeliveries"
NOTICE_KEY = "telegram.poison_notices"
_MAX_PENDING_NOTICES = 50
_UPDATED_BY = "gateway"


class Seen(enum.Enum):
    NEW = "new"
    REDELIVERY = "redelivery"
    DONE = "done"
    POISON = "poison"  # redelivery budget exhausted: marked processed, do not deliver


class UpdateStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_deliveries: int = 5,
        poison_min_age_s: float = 600.0,
    ) -> None:
        self._sf = session_factory
        self._max = max_deliveries
        self._min_age = poison_min_age_s

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
            if done is not None:
                return Seen.DONE
            counts = await self._counts(s, lock=True)
            n = int(counts.get(str(update_id), 1)) + 1
            age_s = await s.scalar(
                select(func.extract("epoch", func.now() - InboundUpdate.received_at)).where(
                    InboundUpdate.update_id == update_id
                )
            )
            if n > self._max and float(age_s or 0) >= self._min_age:
                counts.pop(str(update_id), None)
                await self._save_counts(s, counts)
                await self._add_notice(s, update_id)
                await s.execute(
                    update(InboundUpdate)
                    .where(
                        InboundUpdate.update_id == update_id, InboundUpdate.processed_at.is_(None)
                    )
                    .values(processed_at=func.now())
                )
                await self._advance(s)
                return Seen.POISON
            counts[str(update_id)] = n
            await self._save_counts(s, counts)
            return Seen.REDELIVERY

    async def _flag_dict(self, s: AsyncSession, key: str, *, lock: bool) -> dict[str, Any]:
        await s.execute(
            pg_insert(SystemFlag)
            .values(key=key, value={}, updated_by=_UPDATED_BY)
            .on_conflict_do_nothing(index_elements=[SystemFlag.key])
        )
        q = select(SystemFlag).where(SystemFlag.key == key)
        row = (await s.execute(q.with_for_update() if lock else q)).scalar_one()
        return dict(row.value)

    async def _save_flag_dict(self, s: AsyncSession, key: str, value: dict[str, Any]) -> None:
        await s.execute(update(SystemFlag).where(SystemFlag.key == key).values(value=value))

    async def _counts(self, s: AsyncSession, *, lock: bool) -> dict[str, Any]:
        return await self._flag_dict(s, REDELIVERY_KEY, lock=lock)

    async def _save_counts(self, s: AsyncSession, counts: dict[str, Any]) -> None:
        await self._save_flag_dict(s, REDELIVERY_KEY, counts)

    async def _add_notice(self, s: AsyncSession, update_id: int) -> None:
        received = await s.scalar(
            select(InboundUpdate.received_at).where(InboundUpdate.update_id == update_id)
        )
        if received is None:
            return
        notices = await self._flag_dict(s, NOTICE_KEY, lock=True)
        notices[str(update_id)] = received.isoformat()
        if len(notices) > _MAX_PENDING_NOTICES:  # bounded: keep the newest
            keep = sorted(notices.items(), key=lambda kv: kv[1])[-_MAX_PENDING_NOTICES:]
            notices = dict(keep)
        await self._save_flag_dict(s, NOTICE_KEY, notices)

    async def pending_poison_notices(self) -> list[tuple[int, datetime]]:
        """Poisoned updates whose "please resend" notice is not yet confirmed sent."""
        async with self._sf() as s:
            row = await s.get(SystemFlag, NOTICE_KEY)
            if row is None:
                return []
            out: list[tuple[int, datetime]] = []
            for k, v in dict(row.value).items():
                try:
                    out.append((int(k), datetime.fromisoformat(str(v))))
                except ValueError:
                    continue
            return sorted(out, key=lambda t: t[0])

    async def clear_poison_notice(self, update_id: int) -> None:
        async with self._sf() as s, s.begin():
            notices = await self._flag_dict(s, NOTICE_KEY, lock=True)
            if notices.pop(str(update_id), None) is not None:
                await self._save_flag_dict(s, NOTICE_KEY, notices)

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
            counts = await self._counts(s, lock=True)
            if counts.pop(str(update_id), None) is not None:
                await self._save_counts(s, counts)
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
