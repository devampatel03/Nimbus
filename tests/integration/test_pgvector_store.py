# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P1.3b: pgvector-backed episodic `MemoryStore` over `memory_chunk`
(real Postgres; schema via `Base.metadata.create_all`, same DDL/triggers as
the Alembic migration)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.embedder import DEFAULT_EMBED_DIM
from src.memory.manager import DeterministicStubEmbedder, EpisodicChunk
from src.memory.store import MemoryStore, PgVectorMemoryStore

EMB = DeterministicStubEmbedder()


def _store(session: AsyncSession) -> PgVectorMemoryStore:
    return PgVectorMemoryStore(session, model_id="stub", dim=DEFAULT_EMBED_DIM)


def _chunk(text_: str, **kw: object) -> EpisodicChunk:
    return EpisodicChunk(text=text_, occurred_at=datetime.now(UTC), **kw)  # type: ignore[arg-type]


def test_pgvector_store_satisfies_protocol(db_session: AsyncSession) -> None:
    store: MemoryStore = _store(db_session)
    assert store is not None


@pytest.mark.asyncio
async def test_write_then_search_returns_nearest_first(db_session: AsyncSession) -> None:
    store = _store(db_session)
    session_id = uuid.uuid4()
    texts = ["alpha chunk", "beta chunk", "gamma chunk"]
    ids = await store.write_chunks(session_id, [(_chunk(t), EMB.embed(t)) for t in texts])
    assert len(ids) == 3

    hits = await store.search(EMB.embed("beta chunk"), limit=3)
    assert hits[0].content == "beta chunk"
    assert hits[0].similarity == pytest.approx(1.0, abs=1e-4)
    assert hits[0].session_id == session_id
    assert hits[0].chunk_id == ids[1]
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_every_row_has_provenance(db_session: AsyncSession) -> None:
    store = _store(db_session)
    await store.write_chunks(uuid.uuid4(), [(_chunk("x"), EMB.embed("x"))])
    row = (
        await db_session.execute(
            text(
                "SELECT c.lane, c.confidence, c.created_at, c.source_event_id, "
                "c.model_id, c.dim, e.lane AS elane FROM memory_chunk c "
                "JOIN event e ON e.id = c.source_event_id"
            )
        )
    ).one()
    assert row.lane == "trusted" and row.elane == "trusted"
    assert row.confidence == 1.0
    assert row.created_at is not None and row.source_event_id is not None
    assert (row.model_id, row.dim) == ("stub", DEFAULT_EMBED_DIM)


@pytest.mark.asyncio
async def test_untrusted_chunks_are_stored_unpromoted_and_not_searchable(
    db_session: AsyncSession,
) -> None:
    """R8: untrusted-derived rows never auto-promote; retrieval skips them."""
    store = _store(db_session)
    sid = uuid.uuid4()
    await store.write_chunks(
        sid,
        [
            (_chunk("trusted fact"), EMB.embed("trusted fact")),
            (_chunk("evil fact", lane="untrusted"), EMB.embed("evil fact")),
        ],
    )
    promoted = (
        await db_session.execute(text("SELECT lane, promoted FROM memory_chunk ORDER BY lane"))
    ).all()
    assert [(r.lane, r.promoted) for r in promoted] == [("trusted", True), ("untrusted", False)]

    hits = await store.search(EMB.embed("evil fact"), limit=10)
    assert [h.content for h in hits] == ["trusted fact"]


@pytest.mark.asyncio
async def test_invalidated_chunks_are_not_searchable_and_content_is_append_only(
    db_session: AsyncSession,
) -> None:
    store = _store(db_session)
    [cid] = await store.write_chunks(uuid.uuid4(), [(_chunk("old"), EMB.embed("old"))])

    with pytest.raises((DBAPIError, IntegrityError)):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE memory_chunk SET content = 'new' WHERE id = :i"), {"i": cid}
            )
    with pytest.raises((DBAPIError, IntegrityError)):
        async with db_session.begin_nested():
            await db_session.execute(text("DELETE FROM memory_chunk WHERE id = :i"), {"i": cid})

    await store.invalidate(cid)
    assert await store.search(EMB.embed("old"), limit=5) == []


@pytest.mark.asyncio
async def test_wrong_dimension_is_rejected(db_session: AsyncSession) -> None:
    store = _store(db_session)
    with pytest.raises(ValueError):
        await store.write_chunks(uuid.uuid4(), [(_chunk("x"), [0.1, 0.2])])


@pytest.mark.asyncio
async def test_search_session_filter(db_session: AsyncSession) -> None:
    store = _store(db_session)
    a, b = uuid.uuid4(), uuid.uuid4()
    await store.write_chunks(a, [(_chunk("in a"), EMB.embed("in a"))])
    await store.write_chunks(b, [(_chunk("in b"), EMB.embed("in b"))])
    hits = await store.search(EMB.embed("in a"), limit=5, session_id=b)
    assert [h.content for h in hits] == ["in b"]
