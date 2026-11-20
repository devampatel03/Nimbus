# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Scheduler integration fixtures: a scratch database per module (real
Postgres, ORM-created tables), a session factory, and a clean job/reminder/task
slate before every test. Append-only tables (`event`, `memory_chunk`) cannot be
cleaned, so tests use unique source keys instead."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.settings import settings
from src.memory.schemas import Base
from tests._test_db import PREFIX
from tests.fakes.clock import FakeClock


@pytest.fixture(scope="module")
async def sched_engine() -> AsyncIterator[AsyncEngine]:
    from tests.conftest import _create_db, _drop_db

    name = f"{PREFIX}{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    await _create_db(name)
    base = settings.postgres_dsn.rsplit("/", 1)[0]
    engine = create_async_engine(f"{base}/{name}", pool_size=10)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        await _drop_db(name)


@pytest.fixture
async def sf(sched_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(sched_engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        await s.execute(text("DELETE FROM job"))
        for table in ("reminder", "task"):
            exists = await s.scalar(text("SELECT to_regclass(:t)"), {"t": table})
            if exists:
                await s.execute(text(f"DELETE FROM {table}"))
    yield factory


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
