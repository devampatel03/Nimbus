# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`PgEventSink` against real Postgres (P1.2c `event` table): the UNIQUE
`source_key` makes redelivery a no-op, and pending follow-ons are found."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from src.ingest import Ingestor, PgEventSink
from src.memory.schemas.event import Event as Row
from tests.unit.ingest.fakes import FakeGateway, FakeTranscriber, clock, text_update, voice_update


@pytest.fixture
async def sf(pg_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    conn: AsyncConnection
    async with pg_engine.connect() as conn:
        tx = await conn.begin()
        yield async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        await tx.rollback()


async def test_same_update_twice_one_row(sf: async_sessionmaker[AsyncSession]) -> None:
    calls: list[str] = []
    ing = Ingestor(PgEventSink(sf), FakeGateway(calls), FakeTranscriber(calls), clock=clock)
    a = await ing.store(text_update(4242, "hello\x00 world"))
    b = await ing.store(text_update(4242, "hello world"))
    assert a and b and a.event_id == b.event_id and a.created and not b.created
    async with sf() as s:
        row = (await s.execute(select(Row).where(Row.source_key == "telegram:4242"))).scalar_one()
        n = await s.scalar(
            select(func.count()).select_from(Row).where(Row.source_key == "telegram:4242")
        )
    assert (row.kind, row.lane, row.body) == ("text", "trusted", "hello world")
    assert n == 1


async def test_voice_follow_on_and_recovery_in_postgres(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []
    gw = FakeGateway(calls, files={"fid8801": b"OggS"})
    ing = Ingestor(PgEventSink(sf), gw, FakeTranscriber(calls), clock=clock)
    assert await ing.store(voice_update(8801, untrusted=True))
    sink = PgEventSink(sf)
    pending = await sink.pending_follow_ons(10)
    mine = [p for p in pending if p.meta.get("update_id") == 8801]
    assert len(mine) == 1 and mine[0].lane == "untrusted"
    await ing.recover_pending(limit=1000)
    assert not [p for p in await sink.pending_follow_ons(1000) if p.meta.get("update_id") == 8801]
    async with sf() as s:
        row = (
            await s.execute(select(Row).where(Row.source_key == "telegram:8801:transcript"))
        ).scalar_one()
    assert row.kind == "voice_transcript" and row.lane == "untrusted"
