# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P1.3b/P2.4: pgvector-backed episodic `MemoryStore` over `memory_chunk`
(real Postgres; schema via `Base.metadata.create_all`, same DDL/triggers as
the Alembic migration). The store takes the caller's real `event_id` (MED-1)
and never creates events behind the caller's back."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.store import (
    ChunkIn,
    ChunkNotFoundError,
    EventNotFoundError,
    MemoryStore,
    PgVectorMemoryStore,
)
from tests._memory_helpers import EMB, make_event, query, store_for


def test_pgvector_store_satisfies_protocol(db_session: AsyncSession) -> None:
    store: MemoryStore = store_for(db_session)
    assert store is not None


async def _seed(
    session: AsyncSession, texts: list[str], **ev_kw: object
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    store = store_for(session)
    eid = await store.record_event(make_event(**ev_kw))  # type: ignore[arg-type]
    return eid, await store.add_chunks(eid, [ChunkIn(text=t) for t in texts])


async def test_record_event_is_idempotent_on_source_key(db_session: AsyncSession) -> None:
    store = store_for(db_session)
    ev = make_event("telegram:42")
    a = await store.record_event(ev)
    b = await store.record_event(ev)
    assert a == b
    n = (await db_session.execute(text("SELECT count(*) FROM event"))).scalar_one()
    assert n == 1


async def test_write_then_search_returns_nearest_first(db_session: AsyncSession) -> None:
    eid, ids = await _seed(db_session, ["alpha chunk", "beta chunk", "gamma chunk"])
    hits = await store_for(db_session).search(query("beta chunk", top_k=3))
    assert hits[0].content == "beta chunk"
    assert hits[0].similarity == pytest.approx(1.0, abs=1e-4)
    assert hits[0].chunk_id == ids[1]
    assert hits[0].source_event_id == eid
    assert len(hits) == 3


async def test_store_never_creates_synthetic_events(db_session: AsyncSession) -> None:
    """MED-1: the only event rows are the ones the caller recorded."""
    await _seed(db_session, ["a", "b", "c"], key="telegram:7")
    keys = (await db_session.execute(text("SELECT source_key FROM event"))).scalars().all()
    assert keys == ["telegram:7"]


async def test_add_chunks_unknown_event_is_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(EventNotFoundError):
        await store_for(db_session).add_chunks(uuid.uuid4(), [ChunkIn(text="x")])


async def test_chunk_index_follows_position_and_replay_is_idempotent(
    db_session: AsyncSession,
) -> None:
    store = store_for(db_session)
    eid = await store.record_event(make_event())
    first = await store.add_chunks(eid, [ChunkIn(text="one"), ChunkIn(text="two")])
    again = await store.add_chunks(eid, [ChunkIn(text="one"), ChunkIn(text="two")])
    assert first == again
    rows = (
        await db_session.execute(
            text("SELECT chunk_index, content FROM memory_chunk ORDER BY chunk_index")
        )
    ).all()
    assert [(r.chunk_index, r.content) for r in rows] == [(0, "one"), (1, "two")]


async def test_replay_does_not_re_embed(db_session: AsyncSession) -> None:
    calls: list[int] = []

    class Counting:
        model_id = "counting"
        dim = 1024

        async def aembed_batch(self, texts: list[str]) -> list[list[float]]:
            calls.append(len(texts))
            return [EMB.embed(t) for t in texts]

    store = PgVectorMemoryStore(db_session, embedder=Counting())
    eid = await store.record_event(make_event())
    await store.add_chunks(eid, [ChunkIn(text="x"), ChunkIn(text="y")])
    await store.add_chunks(eid, [ChunkIn(text="x"), ChunkIn(text="y")])
    assert calls == [2]  # one batch call, nothing paid for on replay


async def test_explicit_chunk_index_and_duplicates_rejected(db_session: AsyncSession) -> None:
    store = store_for(db_session)
    eid = await store.record_event(make_event())
    ids = await store.add_chunks(eid, [ChunkIn(text="late", chunk_index=5)])
    assert len(ids) == 1
    with pytest.raises(ValueError):
        await store.add_chunks(
            eid, [ChunkIn(text="a", chunk_index=1), ChunkIn(text="b", chunk_index=1)]
        )


async def test_every_row_has_provenance(db_session: AsyncSession) -> None:
    eid, _ = await _seed(db_session, ["x"])
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
    assert row.created_at is not None and row.source_event_id == eid
    assert (row.model_id, row.dim) == ("DeterministicStubEmbedder", 1024)


async def test_invalidated_chunks_are_not_searchable_and_content_is_append_only(
    db_session: AsyncSession,
) -> None:
    store = store_for(db_session)
    _, [cid] = await _seed(db_session, ["old"])

    with pytest.raises((DBAPIError, IntegrityError)):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE memory_chunk SET content = 'new' WHERE id = :i"), {"i": cid}
            )
    with pytest.raises((DBAPIError, IntegrityError)):
        async with db_session.begin_nested():
            await db_session.execute(text("DELETE FROM memory_chunk WHERE id = :i"), {"i": cid})

    await store.invalidate(cid)
    assert await store.search(query("old", top_k=5)) == []


async def test_invalidate_unknown_id_raises_and_repeat_is_noop(db_session: AsyncSession) -> None:
    store = store_for(db_session)
    with pytest.raises(ChunkNotFoundError):
        await store.invalidate(uuid.uuid4())
    _, [cid] = await _seed(db_session, ["x"])
    await store.invalidate(cid)
    stamp = (await db_session.execute(text("SELECT invalidated_at FROM memory_chunk"))).scalar_one()
    await store.invalidate(cid)  # set-once column: must not be rewritten (INV-3)
    again = (await db_session.execute(text("SELECT invalidated_at FROM memory_chunk"))).scalar_one()
    assert again == stamp


async def test_hnsw_iterative_scan_setting_does_not_leak(db_session: AsyncSession) -> None:
    await _seed(db_session, ["x"])
    before = (await db_session.execute(text("SHOW hnsw.iterative_scan"))).scalar_one()
    await store_for(db_session).search(query("x"))
    after = (await db_session.execute(text("SHOW hnsw.iterative_scan"))).scalar_one()
    assert after == before


async def test_filtering_survives_many_nonmatching_neighbours(db_session: AsyncSession) -> None:
    """A post-filtered HNSW scan would return < limit when the nearest rows are
    all filtered out; iterative scan must keep going."""
    store = store_for(db_session)
    ev_bad = await store.record_event(make_event(lane="untrusted"))
    await store.add_chunks(ev_bad, [ChunkIn(text=f"needle {i}") for i in range(60)])
    await _seed(db_session, ["haystack one", "haystack two"])
    hits = await store.search(query("needle 3", top_k=2))
    assert {h.content for h in hits} == {"haystack one", "haystack two"}
