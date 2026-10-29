# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.9 migration a9d4c6e2f801: commitment_ledger. Applies from empty and from
the previous head, single head, up/down/up, no ORM drift, and the MIGRATED table
carries the same guards as the ORM one (append-only + confirm-needs-approval)."""

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
PREV_HEAD = "f3a9c1d7e5b2"  # pragma: allowlist secret
REVISION = "a9d4c6e2f801"  # pragma: allowlist secret


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


def _has_table(dsn: str) -> bool:
    rows = asyncio.run(_run(dsn, "SELECT to_regclass('public.commitment_ledger')"))
    return rows[0][0] is not None


def test_single_head_is_this_revision() -> None:
    script = ScriptDirectory.from_config(_cfg(settings.postgres_dsn))
    heads = script.get_heads()
    assert len(heads) == 1  # later revisions chain on top of this one (P4.6)
    assert REVISION in {r.revision for r in script.walk_revisions(REVISION, heads[0])}


def test_round_trip_and_no_drift(scratch_dsn: str) -> None:
    cfg = _cfg(scratch_dsn)
    command.upgrade(cfg, "head")
    assert _has_table(scratch_dsn)
    assert asyncio.run(_drift(scratch_dsn)) == []
    command.downgrade(cfg, PREV_HEAD)
    assert not _has_table(scratch_dsn)
    fn = asyncio.run(_run(scratch_dsn, "SELECT 1 FROM pg_proc WHERE proname='nimbus_ledger_guard'"))
    assert fn == []
    command.upgrade(cfg, "head")
    assert _has_table(scratch_dsn)
    assert asyncio.run(_drift(scratch_dsn)) == []


def test_migrated_table_enforces_the_guards(scratch_dsn: str) -> None:
    command.upgrade(_cfg(scratch_dsn), "head")
    asyncio.run(
        _run(
            scratch_dsn,
            "INSERT INTO event (id, source_key, kind, source, lane, occurred_at) VALUES "
            "('00000000-0000-0000-0000-0000000000e1', 'e1', 'text', 'telegram', 'trusted', now())",
        )
    )
    cand = (
        "INSERT INTO commitment_ledger (id, commitment_key, version, status, description, "
        "norm_key, dedupe_key, source_event_id, lane, confidence) VALUES "
        "('00000000-0000-0000-0000-0000000000c1', gen_random_uuid(), 1, 'candidate', 'd', 'n', "
        "'k1', '00000000-0000-0000-0000-0000000000e1', 'trusted', 0.9)"
    )
    asyncio.run(_run(scratch_dsn, cand))
    for sql in (
        "UPDATE commitment_ledger SET description = 'changed'",
        "DELETE FROM commitment_ledger",
        "TRUNCATE commitment_ledger",
        # confirmed with no consumed approval
        (
            "INSERT INTO commitment_ledger (commitment_key, version, supersedes_id, status, "
            "description, norm_key, dedupe_key, source_event_id, lane, confidence, promoted) "
            "SELECT commitment_key, 2, id, 'confirmed', description, 'n', 'k2', source_event_id, "
            "lane, 0.9, true FROM commitment_ledger"
        ),
    ):
        with pytest.raises(Exception):  # noqa: B017 - any DB error is a refusal
            asyncio.run(_run(scratch_dsn, sql))
    assert asyncio.run(_run(scratch_dsn, "SELECT count(*) FROM commitment_ledger")) == [(1,)]
