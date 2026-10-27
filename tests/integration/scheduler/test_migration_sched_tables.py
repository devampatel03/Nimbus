# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.3 migrations: reminder + task tables (d6a2f8b41c73), and the `lane`
server default is gone from graph_edge / commitment / decision (e7b3a9c52d84).
Apply from empty and from the previous head, single head, up/down/up."""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[3]
PREV_HEAD = "c4e8a1d92b37"  # pragma: allowlist secret
SCHED_REV = "d6a2f8b41c73"  # pragma: allowlist secret
LANE_TABLES = ("graph_edge", "commitment", "decision")


def _dsn_for(db: str) -> str:
    return settings.postgres_dsn.rsplit("/", 1)[0] + f"/{db}"


async def _run(dsn: str, sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            res = await conn.execute(text(sql), params)
            return [tuple(r) for r in res.all()] if res.returns_rows else []
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


def _lane_defaults(dsn: str) -> dict[str, object]:
    rows = asyncio.run(
        _run(
            dsn,
            "SELECT table_name, column_default FROM information_schema.columns "
            "WHERE column_name = 'lane' AND table_name IN ('graph_edge','commitment','decision')",
        )
    )
    return {str(r[0]): r[1] for r in rows}


def _tables(dsn: str) -> set[str]:
    rows = asyncio.run(_run(dsn, "SELECT tablename FROM pg_tables WHERE schemaname='public'"))
    return {str(r[0]) for r in rows}


def test_single_head() -> None:
    cfg = _cfg(settings.postgres_dsn)
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def test_from_empty_and_round_trip(scratch_dsn: str) -> None:
    cfg = _cfg(scratch_dsn)
    command.upgrade(cfg, "head")
    assert {"reminder", "task"} <= _tables(scratch_dsn)
    assert set(_lane_defaults(scratch_dsn)) == set(LANE_TABLES)
    assert set(_lane_defaults(scratch_dsn).values()) == {None}

    command.downgrade(cfg, SCHED_REV)  # undo the lane migration only
    assert {"reminder", "task"} <= _tables(scratch_dsn)
    assert all("trusted" in str(v) for v in _lane_defaults(scratch_dsn).values())

    command.downgrade(cfg, PREV_HEAD)
    assert not ({"reminder", "task"} & _tables(scratch_dsn))

    command.upgrade(cfg, "head")
    assert {"reminder", "task"} <= _tables(scratch_dsn)
    assert set(_lane_defaults(scratch_dsn).values()) == {None}


def test_from_previous_head_keeps_existing_rows(scratch_dsn: str) -> None:
    cfg = _cfg(scratch_dsn)
    command.upgrade(cfg, PREV_HEAD)
    assert all("trusted" in str(v) for v in _lane_defaults(scratch_dsn).values())
    sid = uuid.uuid4()
    asyncio.run(
        _run(
            scratch_dsn,
            "INSERT INTO session (id, user_id, mode, started_at, device_id) "
            "VALUES (:i, :u, 'mode_b', now(), 'd')",
            i=sid,
            u=uuid.uuid4(),
        )
    )
    asyncio.run(
        _run(
            scratch_dsn,
            "INSERT INTO commitment (id, source_session, description, status, confidence, "
            "created_at) VALUES (:i, :s, 'x', 'open', 0.5, now())",
            i=uuid.uuid4(),
            s=sid,
        )
    )
    command.upgrade(cfg, "head")
    rows = asyncio.run(_run(scratch_dsn, "SELECT lane FROM commitment"))
    assert rows == [("trusted",)]  # existing rows keep the lane they were written with
    with pytest.raises(Exception, match="lane"):
        asyncio.run(
            _run(
                scratch_dsn,
                "INSERT INTO commitment (id, source_session, description, status, confidence, "
                "created_at) VALUES (:i, :s, 'y', 'open', 0.5, now())",
                i=uuid.uuid4(),
                s=sid,
            )
        )
