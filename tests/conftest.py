# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared fixtures for tests that need real Postgres/Qdrant/Redis instances
(per AGENT_OPERATING_MANUAL.md: measurements must use real dependencies, not
mocks). Requires `docker compose up -d`.
"""

from collections.abc import AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from config.settings import settings
from src.memory.schemas import Base


@pytest.fixture(scope="session")
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.postgres_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(pg_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with pg_engine.connect() as conn:
        outer_tx = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await outer_tx.rollback()


@pytest.fixture
async def qdrant_client() -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    yield client
    for collection in (await client.get_collections()).collections:
        await client.delete_collection(collection.name)
    await client.close()


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    yield client
    async for key in client.scan_iter(match="warm_context:*"):
        await client.delete(key)
    await client.aclose()
