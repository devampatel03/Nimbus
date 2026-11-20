# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""R8 adversarial: `promote()` must be bound to ONE specific, consumed,
unexpired `memory`/`promote` approval for exactly that item, and single-use.
Table-driven; each row is an attack that must NOT promote the chunk."""

import hashlib
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.store import ApprovalRequiredError, ChunkIn, PgVectorMemoryStore
from tests._memory_helpers import make_event, query, store_for


async def approval(
    s: AsyncSession,
    *,
    tool: str = "memory",
    action: str = "promote",
    item: uuid.UUID | None = None,
    status: str = "consumed",
    expired_then_consumed: bool = False,
) -> uuid.UUID:
    aid = uuid.uuid4()
    payload: dict[str, Any] = {"item_id": str(item)} if item else {}
    if expired_then_consumed:  # created 2h ago, expired 1h ago, consumed late (now)
        created, expires = "now() - interval '2 hours'", "now() - interval '1 hour'"
    else:
        created, expires = "now() - interval '1 minute'", "now() + interval '1 hour'"
    await s.execute(
        text(
            "INSERT INTO pending_action (id, tool, action, payload, payload_sha256, nonce, "
            "status, expires_at, created_at, consumed_at) VALUES (:i, :t, :a, "
            "CAST(:p AS jsonb), :h, :n, CAST(:st AS varchar), "
            + expires
            + ", "
            + created
            + ", CASE WHEN CAST(:st AS varchar) = 'consumed' THEN now() END)"
        ),
        {
            "i": aid,
            "t": tool,
            "a": action,
            "p": json.dumps(payload),
            "h": hashlib.sha256(b"z").hexdigest(),
            "n": b"n" * 16,
            "st": status,
        },
    )
    return aid


async def two_untrusted(s: AsyncSession) -> tuple[PgVectorMemoryStore, uuid.UUID, uuid.UUID]:
    store = store_for(s)
    ev = await store.record_event(make_event(lane="untrusted", kind="recorder_transcript"))  # type: ignore[arg-type]
    a, b = await store.add_chunks(ev, [ChunkIn(text="evil one"), ChunkIn(text="evil two")])
    return store, a, b


async def promoted_count(s: AsyncSession) -> int:
    return int(
        (await s.execute(text("SELECT count(*) FROM memory_chunk WHERE promoted"))).scalar_one()
    )


async def _unknown(s: AsyncSession, a: uuid.UUID, b: uuid.UUID) -> uuid.UUID:
    return uuid.uuid4()


# (id, builder(session, chunk_a, chunk_b) -> approval id used to promote chunk_a)
CASES: list[tuple[str, Any]] = [
    ("wrong_action", lambda s, a, b: approval(s, action="draft", item=a)),
    ("wrong_tool", lambda s, a, b: approval(s, tool="gmail", item=a)),
    ("unconsumed_pending", lambda s, a, b: approval(s, item=a, status="pending")),
    ("expired_then_consumed", lambda s, a, b: approval(s, item=a, expired_then_consumed=True)),
    ("other_item_id", lambda s, a, b: approval(s, item=b)),
    ("no_item_binding", lambda s, a, b: approval(s)),
    ("unknown_approval", _unknown),
]


@pytest.mark.parametrize(("name", "build"), CASES, ids=[c[0] for c in CASES])
async def test_bad_approval_never_promotes(db_session: AsyncSession, name: str, build: Any) -> None:
    store, a, b = await two_untrusted(db_session)
    aid = await build(db_session, a, b)
    with pytest.raises(ApprovalRequiredError):
        await store.promote(a, approval_id=aid)
    assert await promoted_count(db_session) == 0
    assert await store.search(query("evil")) == []


async def test_one_approval_cannot_promote_a_second_item(db_session: AsyncSession) -> None:
    store, a, b = await two_untrusted(db_session)
    aid = await approval(db_session, item=a)
    await store.promote(a, approval_id=aid)
    with pytest.raises(ApprovalRequiredError):
        await store.promote(b, approval_id=aid)  # replay on the second item
    assert {h.content for h in await store.search(query("evil"))} == {"evil one"}


async def test_same_approval_cannot_promote_the_same_chunk_twice(db_session: AsyncSession) -> None:
    store, a, _ = await two_untrusted(db_session)
    aid = await approval(db_session, item=a)
    await store.promote(a, approval_id=aid)
    with pytest.raises(ApprovalRequiredError):
        await store.promote(a, approval_id=aid)


async def test_invalidated_chunk_is_not_promotable(db_session: AsyncSession) -> None:
    store, a, _ = await two_untrusted(db_session)
    await store.invalidate(a)
    with pytest.raises(ApprovalRequiredError):
        await store.promote(a, approval_id=await approval(db_session, item=a))
    row = (
        await db_session.execute(text("SELECT promoted FROM memory_chunk WHERE id = :i"), {"i": a})
    ).scalar_one()
    assert row is False


async def test_correct_approval_promotes_exactly_that_item(db_session: AsyncSession) -> None:
    store, a, _ = await two_untrusted(db_session)
    await store.promote(a, approval_id=await approval(db_session, item=a))
    got = await store.search(query("evil"))
    assert [h.content for h in got] == ["evil one"]
    assert got[0].untrusted is True  # promotion never rewrites the lane


async def test_db_unique_index_blocks_approval_reuse_even_with_raw_sql(
    db_session: AsyncSession,
) -> None:
    """Defence in depth under the store: one approval id on two chunks is
    rejected by the database itself."""
    _, a, b = await two_untrusted(db_session)
    aid = await approval(db_session, item=a)
    sql = "UPDATE memory_chunk SET promoted = true, promoted_approval_id = :a WHERE id = :i"
    await db_session.execute(text(sql), {"a": aid, "i": a})
    with pytest.raises((IntegrityError, DBAPIError)):
        async with db_session.begin_nested():
            await db_session.execute(text(sql), {"a": aid, "i": b})
