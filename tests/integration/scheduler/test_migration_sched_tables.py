# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.3 migration d6a2f8b41c73: reminder + task tables. Applies from empty and
from the previous head, single head, up/down/up, and the ORM matches."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from config.settings import settings
from src.memory.schemas import Base

ROOT = Path(__file__).resolve().parents[3]
PREV_HEAD = "c4e8a1d92b37"  # pragma: allowlist secret
NEW_TABLES = {"reminder", "task"}


def _dsn_for(db: str) -> str:
    return settings.postgres_dsn.rsplit("/", 1)[0] + f"/{db}"


async def _run(dsn: str, sql: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            res = await conn.execute(text(sql))
            return [tuple(r) for r in res.all()] if res.returns_rows else []
    finally:
        await engine.dispose()


async def _drift(dsn: str) -> list[object]:
    engine = create_async_engine(dsn, poolclass=NullPool)

    def _work(conn: Connection) -> list[object]:
        return list(compare_metadata(MigrationContext.configure(conn), Base.metadata))

    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_work)
    finally:
        await engine.dispose()


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    name = f"nimbus_migtest_{uuid.uuid4().hex[:10]}"
    asyncio.run(_run(_dsn_for("postgres"), f'CREATE DATABASE "{name}"'))
    try:
        yield _dsn_for(name)
    finally:
        asyncio.run(_run(_dsn_for("postgres"), f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _cfg(dsn: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def _tables(dsn: str) -> set[str]:
    rows = asyncio.run(_run(dsn, "SELECT tablename FROM pg_tables WHERE schemaname='public'"))
    return {str(r[0]) for r in rows}


def test_single_head() -> None:
    cfg = _cfg(settings.postgres_dsn)
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def test_from_empty_matches_orm_and_round_trips(scratch_dsn: str) -> None:
    cfg = _cfg(scratch_dsn)
    command.upgrade(cfg, "head")
    assert NEW_TABLES <= _tables(scratch_dsn)
    assert asyncio.run(_drift(scratch_dsn)) == []

    command.downgrade(cfg, PREV_HEAD)
    assert not (NEW_TABLES & _tables(scratch_dsn))

    command.upgrade(cfg, "head")
    assert NEW_TABLES <= _tables(scratch_dsn)
    assert asyncio.run(_drift(scratch_dsn)) == []


def test_from_previous_head_keeps_existing_rows(scratch_dsn: str) -> None:
    cfg = _cfg(scratch_dsn)
    command.upgrade(cfg, PREV_HEAD)
    asyncio.run(
        _run(
            scratch_dsn,
            "INSERT INTO event (source_key, kind, source, lane, occurred_at) "
            "VALUES ('keep-me', 'text', 'telegram', 'trusted', now())",
        )
    )
    command.upgrade(cfg, "head")
    assert NEW_TABLES <= _tables(scratch_dsn)
    rows = asyncio.run(_run(scratch_dsn, "SELECT source_key FROM event"))
    assert rows == [("keep-me",)]
