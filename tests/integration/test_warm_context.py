# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""T1.7 acceptance criterion: "Retrieval within turn 100ms max, measured."

This measures `get_warm_context` (the Mode A read path) against a real
Redis instance after a real `refresh_warm_context` population — not a mock,
per AGENT_OPERATING_MANUAL.md 3.3. `refresh_warm_context` itself does the
expensive retrieval (Qdrant + Postgres) and is explicitly NOT on the Mode A
path (INV-4) — it runs post-turn. Only the cache read is budget-checked.
"""

import time
import uuid
from datetime import UTC, datetime

import pytest
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.manager import EpisodicChunk, MemoryManager, SessionState
from src.memory.schemas.session import Session

WARM_CONTEXT_BUDGET_SECONDS = 0.1  # T1.7: 100ms max


@pytest.mark.asyncio
async def test_get_warm_context_within_100ms_budget(
    db_session: AsyncSession, qdrant_client: AsyncQdrantClient, redis_client: Redis
) -> None:
    now = datetime.now(UTC)
    session_row = Session(
        user_id=uuid.uuid4(), mode="mode_a", started_at=now, device_id="test-device"
    )
    db_session.add(session_row)
    await db_session.flush()

    manager = MemoryManager(pg_session=db_session, qdrant=qdrant_client, redis=redis_client)
    text = "Discussed the Q3 roadmap slip."
    await manager.write_episodic(session_row.id, [EpisodicChunk(text=text, occurred_at=now)])
    await manager.refresh_warm_context(
        SessionState(session_id=session_row.id, user_id=uuid.uuid4(), query_text=text)
    )

    samples = []
    for _ in range(5):
        start = time.perf_counter()
        warm_slice = await manager.get_warm_context(session_row.id)
        samples.append(time.perf_counter() - start)
        assert warm_slice is not None

    assert (
        max(samples) < WARM_CONTEXT_BUDGET_SECONDS
    ), f"get_warm_context exceeded the 100ms Mode A budget: {max(samples) * 1000:.1f}ms"


@pytest.mark.asyncio
async def test_get_warm_context_cache_miss_is_fast_and_returns_none(
    db_session: AsyncSession, qdrant_client: AsyncQdrantClient, redis_client: Redis
) -> None:
    manager = MemoryManager(pg_session=db_session, qdrant=qdrant_client, redis=redis_client)

    start = time.perf_counter()
    result = await manager.get_warm_context(uuid.uuid4())
    elapsed = time.perf_counter() - start

    assert result is None
    assert elapsed < WARM_CONTEXT_BUDGET_SECONDS
