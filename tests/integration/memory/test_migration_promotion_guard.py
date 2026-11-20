# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P4.6 migration e5b1c7a3d902: promotion guard triggers, reversible, one head,
and the frozen function body matches the ORM-side copy."""

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.memory.schemas.triggers import PROMOTION_GUARD_FN_SQL
from tests.integration.test_migration_single_use_approval import _admin, _cfg, _dsn_for

TRIGGERS = {"trg_memory_chunk_promotion_guard", "trg_graph_edge_promotion_guard"}
PREV = "a9d4c6e2f801"  # pragma: allowlist secret


@pytest.fixture
def dsn() -> Iterator[str]:
    name = f"nimbus_migpg_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        yield _dsn_for(name)
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _triggers(dsn: str) -> set[str]:
    engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


async def _fn_body(dsn: str) -> str:
    engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT prosrc FROM pg_proc WHERE proname = 'nimbus_promotion_guard'")
            )
            return str(row.scalar_one())
    finally:
        await engine.dispose()


def test_promotion_guard_up_down_up(dsn: str) -> None:
    cfg = _cfg(dsn)
    command.upgrade(cfg, PREV)
    assert not TRIGGERS & asyncio.run(_triggers(dsn))
    command.upgrade(cfg, "head")
    assert TRIGGERS <= asyncio.run(_triggers(dsn))
    command.downgrade(cfg, PREV)
    assert not TRIGGERS & asyncio.run(_triggers(dsn))
    command.upgrade(cfg, "head")
    assert TRIGGERS <= asyncio.run(_triggers(dsn))


def test_migrated_function_matches_orm_copy(dsn: str) -> None:
    command.upgrade(_cfg(dsn), "head")
    body = asyncio.run(_fn_body(dsn))
    orm = PROMOTION_GUARD_FN_SQL.split("$fn$")[1]
    assert " ".join(body.split()) == " ".join(orm.split())
