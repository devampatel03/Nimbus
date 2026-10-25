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

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from config.settings import settings
from src.llm.client import ChatMessage
from src.memory.schemas import Base

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
