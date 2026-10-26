# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P2.4: concurrent writers (committed transactions, separate connections).

Uses its own scratch database because the append-only tables cannot be
cleaned up (no DELETE/TRUNCATE, INV-3) and must not leak rows into other
tests' global assertions.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.ext.asyncio import AsyncSession as Session
from sqlalchemy.pool import NullPool

from config.settings import settings
from src.memory.schemas import Base
from src.memory.store import ChunkIn
from tests._memory_helpers import make_event, query, store_for


def _dsn(db: str) -> str:
    return settings.postgres_dsn.rsplit("/", 1)[0] + f"/{db}"


@pytest.fixture
async def scratch_engine() -> AsyncIterator[AsyncEngine]:
    name = f"nimbus_p24_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as c:
        await c.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(_dsn(name), pool_size=10)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_concurrent_add_chunks_same_event_is_idempotent(scratch_engine: AsyncEngine) -> None:
    async with Session(scratch_engine) as s, s.begin():
        eid = await store_for(s).record_event(make_event("telegram:1"))

    async def writer() -> list[uuid.UUID]:
        async with Session(scratch_engine) as s, s.begin():
            return await store_for(s).add_chunks(eid, [ChunkIn(text=f"c{i}") for i in range(5)])

    results = await asyncio.gather(*(writer() for _ in range(8)))
    assert all(r == results[0] for r in results)  # everyone sees the same ids
    async with scratch_engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM memory_chunk"))).scalar_one()
    assert n == 5  # exactly one row per (event, chunk_index)


async def test_concurrent_record_event_same_source_key(scratch_engine: AsyncEngine) -> None:
    async def rec() -> uuid.UUID:
        async with Session(scratch_engine) as s, s.begin():
            return await store_for(s).record_event(make_event("telegram:777"))

    ids = await asyncio.gather(*(rec() for _ in range(10)))
    assert len(set(ids)) == 1
    async with scratch_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM event"))).scalar_one() == 1


async def test_concurrent_writes_to_distinct_events_and_search(
    scratch_engine: AsyncEngine,
) -> None:
    async def one(i: int) -> None:
        async with Session(scratch_engine) as s, s.begin():
            st = store_for(s)
            eid = await st.record_event(make_event(f"telegram:{1000 + i}"))
            await st.add_chunks(eid, [ChunkIn(text=f"doc {i} a"), ChunkIn(text=f"doc {i} b")])

    await asyncio.gather(*(one(i) for i in range(12)))
    async with Session(scratch_engine) as s:
        hits = await store_for(s).search(query("doc 3 a", top_k=3))
    assert hits[0].content == "doc 3 a"
    async with scratch_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM memory_chunk"))).scalar_one() == 24
