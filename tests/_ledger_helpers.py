# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared helpers for the P3.9 commitment-ledger tests (real Postgres, real
approval flow; only Telegram, the clock and the model are fakes)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.commitments import CommitmentIntake, CommitmentLedgerStore
from src.memory.schemas.event import Event
from tests._approval_helpers import Harness, build_harness

SF = async_sessionmaker[AsyncSession]
T_MSG = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)  # Thursday 11:30 IST
BODY = "I'll send the budget deck to Priya by Friday"


async def make_event(
    sf: SF, body: str | None = BODY, *, lane: str = "trusted", kind: str = "text"
) -> uuid.UUID:
    async with sf() as s, s.begin():
        e = Event(
            source_key=f"t:{uuid.uuid4()}",
            kind=kind,
            source="telegram",
            lane=lane,
            body=body,
            meta={},
            occurred_at=T_MSG,
        )
        s.add(e)
        await s.flush()
        return e.id


class LedgerHarness:
    def __init__(self, sf: SF, **settings: Any) -> None:
        self.h: Harness = build_harness(sf, **settings)
        self.sf = sf
        self.ledger = CommitmentLedgerStore(sf, self.h.clock)
        self.intake = CommitmentIntake(self.ledger, self.h.service)

        async def confirm(candidate_id: uuid.UUID, approval_id: uuid.UUID) -> None:
            await self.ledger.confirm(candidate_id, approval_id)

        self.h.service.bind_confirmer(confirm)
