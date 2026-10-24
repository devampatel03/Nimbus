# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Alembic baseline (P1.2b): migrations apply from empty, match the ORM
exactly, and round-trip. Runs against a throwaway scratch database on the real
Postgres; the dev database is never touched.
"""

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import src.personality.agent_character
import src.personality.user_profile  # noqa: F401
from config.settings import settings
from src.memory.schemas import Base

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "graph_node",
    "graph_edge",
    "session",
    "person",
    "commitment",
    "decision",
    "agent_stance",
    "user_trait",
    "trait_evidence",
}


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


async def _inspect(dsn: str) -> tuple[list[object], set[str]]:
    engine = create_async_engine(dsn, poolclass=NullPool)

    def _work(conn: Connection) -> tuple[list[object], set[str]]:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        return list(diff), {r[0] for r in rows}

    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_work)
    finally:
        await engine.dispose()


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    name = f"nimbus_migtest_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        yield _dsn_for(name)
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _cfg(dsn: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def test_upgrade_matches_orm_and_round_trips(scratch_dsn: str) -> None:
    cfg = _cfg(scratch_dsn)

    command.upgrade(cfg, "head")
    diff, tables = asyncio.run(_inspect(scratch_dsn))
    assert EXPECTED_TABLES <= tables
    assert diff == [], f"migrations drift from ORM: {diff}"

    command.downgrade(cfg, "base")
    _, tables = asyncio.run(_inspect(scratch_dsn))
    assert not (EXPECTED_TABLES & tables)

    command.upgrade(cfg, "head")
    diff, tables = asyncio.run(_inspect(scratch_dsn))
    assert EXPECTED_TABLES <= tables
    assert diff == []
