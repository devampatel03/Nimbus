# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared fixtures for tests that need real Postgres instance
(per AGENT_OPERATING_MANUAL.md: measurements must use real dependencies, not
mocks). Requires `docker compose up -d`.
"""

import asyncio
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from config.settings import settings
from src.memory.schemas import Base
from tests._test_db import PREFIX, is_stale, is_valid_name, require_valid_name

# --- Per-session throwaway database (INFRA-1) -------------------------------
# Every pytest session gets its own database `nimbus_test_<epoch>_<pid>_<uuid8>`
# and repoints the shared `settings.postgres_db` at it BEFORE any `src` module
# that reads the DSN is imported (src/db/postgres.py builds its engine from the
# DSN at import). Neither `src.llm.client` nor `src.memory.schemas`, imported
# above, touches the DSN; test modules (collected afterwards) do.
# Tests that derive scratch DB DSNs via `settings.postgres_dsn.rsplit("/", 1)`
# therefore keep working unchanged. Concurrent sessions can no longer drop each
# other's tables. Requires a Postgres role with CREATEDB.

_ENV_VAR = "NIMBUS_TEST_SESSION_DB"
_session_db: str | None = None


_warnings: list[str] = []


def _warn(msg: str) -> None:
    # Output at conftest import is swallowed by pytest's early capture, so keep
    # the message and print it in the terminal summary (and now, for late calls).
    _warnings.append(msg)
    print(f"WARNING: {msg}", file=sys.stderr)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for msg in _warnings:
        terminalreporter.write_line(f"WARNING: {msg}", yellow=True)


async def _admin_connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        database="postgres",
        timeout=10,
    )


async def _create_db(name: str) -> None:
    require_valid_name(name)
    conn = await _admin_connect()
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_db(name: str) -> None:
    require_valid_name(name)
    conn = await _admin_connect()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


async def _drop_stale() -> None:
    """Best-effort: drop databases left by killed runs.

    Candidates are selected with an escaped LIKE and then re-checked against the
    strict name regex and the 6h age rule before any DDL. Caveat: a live session
    older than 6h, or >6h of clock skew, can have its database dropped.
    """
    conn = await _admin_connect()
    try:
        rows = await conn.fetch(
            r"SELECT datname FROM pg_database WHERE datname LIKE $1 ESCAPE '\'",
            PREFIX.replace("_", r"\_") + "%",
        )
        now = time.time()
        for row in rows:
            name = row["datname"]
            if not is_stale(name, now):
                continue
            try:
                await conn.execute(
                    f'DROP DATABASE IF EXISTS "{require_valid_name(name)}" WITH (FORCE)'
                )
            except Exception as exc:  # noqa: BLE001
                _warn(f"could not drop stale test database {name}: {type(exc).__name__}")
    finally:
        await conn.close()


def _activate_session_db() -> None:
    """Point `settings` at a throwaway DB; never fall back to the shared one.

    Created lazily-in-spirit: if Postgres is unreachable we warn once and still
    point at the (never created) name, so DB tests fail clearly while pure unit
    tests keep running. xdist workers have a different pid than the controller,
    so each creates (and drops) its own database.
    """
    global _session_db
    if _session_db is not None:
        return
    # Some tests do `from tests.conftest import ...`, which imports this file a
    # second time under another module name. Share state through the process
    # environment so that copy reuses (and never creates/drops) the database.
    existing = os.environ.get(_ENV_VAR, "")
    if is_valid_name(existing) and f"_{os.getpid()}_" in existing:
        settings.postgres_db = existing
        return
    name = f"{PREFIX}{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    require_valid_name(name)
    os.environ[_ENV_VAR] = name
    settings.postgres_db = name
    try:
        asyncio.run(_create_db(name))
    except Exception as exc:  # noqa: BLE001
        _warn(
            f"could not create throwaway test database {name} ({type(exc).__name__}); "
            "DB-backed tests will fail, unit tests still run"
        )
        return
    _session_db = name
    try:
        asyncio.run(_drop_stale())
    except Exception as exc:  # noqa: BLE001
        _warn(f"stale test-database cleanup skipped for {name}: {type(exc).__name__}")


def pytest_unconfigure(config: pytest.Config) -> None:
    global _session_db
    if _session_db is None:
        return
    name, _session_db = _session_db, None
    os.environ.pop(_ENV_VAR, None)
    try:
        asyncio.run(_drop_db(name))
    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        _warn(f"could not drop throwaway test database {name}: {type(exc).__name__}")


_activate_session_db()


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
