# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.3-enable migration b2f6d4a8c1e7: the create-dedupe indexes compare titles
case-insensitively; reversible (up/down/up), single head, and the upgrade
refuses (instead of deleting data) when existing rows already collide."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.test_migration_single_use_approval import _admin, _cfg, _dsn_for

REV = "b2f6d4a8c1e7"  # pragma: allowlist secret
PREV = "e5b1c7a3d902"  # pragma: allowlist secret
NEW = {"uq_reminder_source_ltitle_at", "uq_task_source_ltitle"}
OLD = {"uq_reminder_source_title_at", "uq_task_source_title"}


@pytest.fixture
def dsn() -> Iterator[str]:
    name = f"nimbus_migcd_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        yield _dsn_for(name)
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _sql(dsn: str, *stmts: str) -> list[object]:
    engine = create_async_engine(dsn, poolclass=NullPool)
    out: list[object] = []
    try:
        async with engine.begin() as conn:
            for s in stmts:
                res = await conn.execute(text(s))
                out.append(res.fetchall() if res.returns_rows else None)
        return out
    finally:
        await engine.dispose()


def _indexes(dsn: str) -> set[str]:
    rows = asyncio.run(
        _sql(dsn, "SELECT indexname FROM pg_indexes WHERE tablename IN ('reminder', 'task')")
    )[0]
    assert isinstance(rows, list)
    return {r[0] for r in rows}


def test_single_head_is_the_new_revision(dsn: str) -> None:
    heads = ScriptDirectory.from_config(_cfg(dsn)).get_heads()
    assert heads == [REV]


def test_up_down_up(dsn: str) -> None:
    cfg = _cfg(dsn)
    command.upgrade(cfg, PREV)
    assert OLD <= _indexes(dsn) and not NEW & _indexes(dsn)
    command.upgrade(cfg, "head")
    assert NEW <= _indexes(dsn) and not OLD & _indexes(dsn)
    command.downgrade(cfg, PREV)
    assert OLD <= _indexes(dsn) and not NEW & _indexes(dsn)
    command.upgrade(cfg, "head")
    assert NEW <= _indexes(dsn)


EVENT = (
    "INSERT INTO event (id, source_key, kind, source, lane, body, meta, occurred_at) "
    "VALUES ('{eid}', 'mig:{eid}', 'text', 'telegram', 'trusted', 'x', '{{}}', now())"
)


def test_upgrade_refuses_existing_case_variant_duplicates(dsn: str) -> None:
    cfg = _cfg(dsn)
    command.upgrade(cfg, PREV)
    eid = uuid.uuid4()
    asyncio.run(
        _sql(
            dsn,
            EVENT.format(eid=eid),
            f"INSERT INTO task (title, source_event_id) VALUES ('Milk', '{eid}'), ('milk', '{eid}')",
        )
    )
    with pytest.raises(Exception, match="case-variant duplicate"):
        command.upgrade(cfg, "head")
    rows = asyncio.run(_sql(dsn, "SELECT count(*) FROM task"))[0]
    assert isinstance(rows, list) and rows[0][0] == 2  # nothing deleted
    assert OLD <= _indexes(dsn)
