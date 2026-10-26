# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared fixtures for tests that need real Postgres instance
(per AGENT_OPERATING_MANUAL.md: measurements must use real dependencies, not
mocks). Requires `docker compose up -d`.

Also home to `FakeLLMClient` (this session): API-layer tests
(`test_auth.py`/`test_chat.py`/`test_ws.py`) exercise `/chat` end-to-end
through `src/orchestrator/graph.py`'s `think`/`respond`, which now call a
real `LLMClient` by default -- this fake stands in so those tests stay
hermetic (no real vLLM server needed), reusing the same keyword-heuristic
tool-proposal logic `tests/unit/test_graph.py`'s own fake uses.
"""

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from config.settings import settings
from src.llm.client import ChatMessage
from src.memory.schemas import Base

# --- Per-session throwaway database (INFRA-1) -------------------------------
# Every pytest session gets its own database `nimbus_test_<epoch>_<pid>_<uuid8>`
# and repoints the shared `settings.postgres_db` at it BEFORE any `src` module
# that reads the DSN is imported (src/db/postgres.py builds its engine from the
# DSN at import). Neither `src.llm.client` nor `src.memory.schemas`, imported
# above, touches the DSN; test modules (collected afterwards) do.
# Tests that derive scratch DB DSNs via `settings.postgres_dsn.rsplit("/", 1)`
# therefore keep working unchanged. Concurrent sessions can no longer drop each
# other's tables. Requires a Postgres role with CREATEDB.

_TEST_DB_PREFIX = "nimbus_test_"
_STALE_AFTER_SECONDS = 6 * 3600
_ENV_VAR = "NIMBUS_TEST_SESSION_DB"
_session_db: str | None = None


async def _admin_connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        database="postgres",
    )


async def _create_db(name: str) -> None:
    conn = await _admin_connect()
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_db(name: str) -> None:
    conn = await _admin_connect()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


async def _drop_stale() -> None:
    """Best-effort: drop databases left by killed runs (name embeds creation epoch)."""
    conn = await _admin_connect()
    try:
        rows = await conn.fetch(
            "SELECT datname FROM pg_database WHERE datname LIKE $1", _TEST_DB_PREFIX + "%"
        )
        now = time.time()
        for row in rows:
            name = row["datname"]
            try:
                created = int(name[len(_TEST_DB_PREFIX) :].split("_", 1)[0])
            except ValueError:
                continue
            if now - created > _STALE_AFTER_SECONDS:
                try:
                    await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                except Exception:  # noqa: BLE001, S110
                    pass
    finally:
        await conn.close()


def _activate_session_db() -> None:
    global _session_db
    if _session_db is not None or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    # Some tests do `from tests.conftest import ...`, which imports this file a
    # second time under another module name. Share state through the process
    # environment so that copy reuses (and never creates/drops) the database.
    existing = os.environ.get(_ENV_VAR, "")
    if existing and f"_{os.getpid()}_" in existing:
        settings.postgres_db = existing
        return
    name = f"{_TEST_DB_PREFIX}{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    asyncio.run(_create_db(name))
    _session_db = name
    os.environ[_ENV_VAR] = name
    settings.postgres_db = name
    try:
        asyncio.run(_drop_stale())
    except Exception:  # noqa: BLE001, S110
        pass


def pytest_unconfigure(config: pytest.Config) -> None:
    global _session_db
    if _session_db is None:
        return
    name, _session_db = _session_db, None
    os.environ.pop(_ENV_VAR, None)
    try:
        asyncio.run(_drop_db(name))
    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        print(f"WARNING: could not drop throwaway test database {name}: {type(exc).__name__}")


_activate_session_db()

_CALENDAR_TRIGGERS = ("schedule", "meeting", "calendar event")
_TASK_TRIGGERS = ("remind me", "add a task", "todo", "to-do")


class FakeLLMClient:
    """`LLMClient` double: `structured` mirrors the earlier sessions'
    keyword heuristic for `_ToolProposal`-shaped schemas (has a `tool`
    field) and auto-approves `_LLMJudgment`-shaped schemas (has an
    `approved` field); `complete` echoes any tool-results system note
    it's given, else returns a fixed reply.
    """

    async def structured(
        self, messages: list[ChatMessage], *, schema: type, temperature: float = 0.0
    ):
        fields = schema.model_fields
        if "approved" in fields:
            return schema(approved=True, reason="fake: approved")
        if "tool" not in fields:
            return schema()
        user_text = messages[-1]["content"].lower()
        title = messages[-1]["content"].strip()[:80] or "untitled"
        if any(trigger in user_text for trigger in _CALENDAR_TRIGGERS):
            return schema(
                tool="calendar", action="create", params={"title": title, "start": "", "end": ""}
            )
        if any(trigger in user_text for trigger in _TASK_TRIGGERS):
            return schema(tool="tasks", action="create", params={"title": title})
        return schema(tool=None, action=None, params={})

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        tool_notes = [
            m["content"]
            for m in messages
            if m["role"] == "system" and m["content"].startswith("Tool execution results")
        ]
        if tool_notes:
            return f"Done. {tool_notes[0]}"
        return "Got it."

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ):
        raise NotImplementedError


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
