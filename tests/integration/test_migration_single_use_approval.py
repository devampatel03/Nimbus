# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P2.4 round 1 (H1): single-use promotion approvals at the database level.

One approval id may stamp at most one memory_chunk and at most one graph_edge
(partial UNIQUE indexes). Upgrading a populated database that already holds a
duplicate non-null promoted_approval_id would fail; none can exist legitimately.
"""

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from config.settings import settings

ROOT = Path(__file__).resolve().parents[2]
NEW_INDEXES = {"uq_memory_chunk_promoted_approval", "uq_graph_edge_promoted_approval"}


def _dsn_for(db: str) -> str:
    return settings.postgres_dsn.rsplit("/", 1)[0] + f"/{db}"


async def _admin(sql: str) -> None:
    engine = create_async_engine(
        _dsn_for("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def _cfg(dsn: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


@pytest.fixture
def dsn() -> Iterator[str]:
    name = f"nimbus_migsu_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        yield _dsn_for(name)
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _indexdefs(dsn: str) -> dict[str, str]:
    engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
            )
            return {r[0]: r[1] for r in rows}
    finally:
        await engine.dispose()


def test_alembic_has_a_single_head() -> None:
    heads = ScriptDirectory.from_config(_cfg("postgresql+asyncpg://unused/unused")).get_heads()
    assert len(heads) == 1


def test_single_use_indexes_up_down_up(dsn: str) -> None:
    cfg = _cfg(dsn)
    command.upgrade(cfg, "head")
    defs = asyncio.run(_indexdefs(dsn))
    assert NEW_INDEXES <= defs.keys()
    for name in NEW_INDEXES:
        assert "UNIQUE" in defs[name]
        assert "promoted_approval_id IS NOT NULL" in defs[name]
    command.downgrade(cfg, "b7d2e9f40a15")  # pragma: allowlist secret  (before c4e8a1d92b37)
    assert not NEW_INDEXES & asyncio.run(_indexdefs(dsn)).keys()
    command.upgrade(cfg, "head")
    assert NEW_INDEXES <= asyncio.run(_indexdefs(dsn)).keys()
