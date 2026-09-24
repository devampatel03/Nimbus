# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""MEM-1: integration test writing and retrieving across the working
(Redis), episodic (Qdrant) and semantic (Postgres graph) tiers through
`MemoryManager`, per AGENT_OPERATING_MANUAL.md 3.3's "measure with real
dependencies" rule.
"""

import uuid
from datetime import UTC, datetime

import pytest
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.contracts.memory_query import MemoryQuery
from src.memory.manager import (
    DeterministicStubEmbedder,
    EpisodicChunk,
    MemoryManager,
    SemanticFact,
    SessionState,
)
from src.memory.schemas.graph import GraphNode
from src.memory.schemas.session import Session


async def _make_session_and_node(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    session_row = Session(
        user_id=uuid.uuid4(), mode="mode_b", started_at=now, device_id="test-device"
    )
    node = GraphNode(entity_type="person", canonical_name="Priya", created_at=now)
    db_session.add_all([session_row, node])
    await db_session.flush()
    return session_row.id, node.id


@pytest.mark.asyncio
async def test_write_and_query_episodic_roundtrip(
    db_session: AsyncSession, qdrant_client: AsyncQdrantClient, redis_client: Redis
) -> None:
    session_id, _ = await _make_session_and_node(db_session)
    manager = MemoryManager(
        pg_session=db_session,
        qdrant=qdrant_client,
        redis=redis_client,
        embedder=DeterministicStubEmbedder(),
    )

    chunk_text = "The client wants the launch moved to next Tuesday."
    ids = await manager.write_episodic(
        session_id, [EpisodicChunk(text=chunk_text, occurred_at=datetime.now(UTC))]
    )
    assert len(ids) == 1

    results = await manager.query(
        chunk_text, MemoryQuery(user_id=uuid.uuid4(), query_text=chunk_text, top_k=5)
    )
    assert any(r.content == chunk_text for r in results)
    top = next(r for r in results if r.content == chunk_text)
    assert top.source_session == session_id
    assert top.provenance  # INV-5: non-null
    assert top.confidence == 1.0


@pytest.mark.asyncio
async def test_write_semantic_inserts_then_supersedes(
    db_session: AsyncSession, qdrant_client: AsyncQdrantClient, redis_client: Redis
) -> None:
    session_id, node_id = await _make_session_and_node(db_session)
    manager = MemoryManager(
        pg_session=db_session,
        qdrant=qdrant_client,
        redis=redis_client,
        embedder=DeterministicStubEmbedder(),
    )
    now = datetime.now(UTC)

    [edge_v1] = await manager.write_semantic(
        [
            SemanticFact(
                subject_id=node_id,
                predicate="works_on",
                object_literal="Project Nimbus",
                valid_from=now,
                source_session=session_id,
                confidence=0.9,
            )
        ]
    )
    assert edge_v1.object_literal == "Project Nimbus"
    assert edge_v1.valid_to is None

    [edge_v2] = await manager.write_semantic(
        [
            SemanticFact(
                subject_id=node_id,
                predicate="works_on",
                object_literal="Project Aurora",
                valid_from=now,
                source_session=session_id,
                confidence=0.95,
            )
        ]
    )

    assert edge_v2.id != edge_v1.id
    assert edge_v2.object_literal == "Project Aurora"

    # Old edge is superseded, not mutated (INV-3).
    refreshed_v1 = await db_session.get(type(edge_v1), edge_v1.id)
    assert refreshed_v1 is not None
    assert refreshed_v1.object_literal == "Project Nimbus"
    assert refreshed_v1.valid_to is not None
    assert refreshed_v1.superseded_by == edge_v2.id


@pytest.mark.asyncio
async def test_write_semantic_is_idempotent_for_an_unchanged_fact(
    db_session: AsyncSession, qdrant_client: AsyncQdrantClient, redis_client: Redis
) -> None:
    session_id, node_id = await _make_session_and_node(db_session)
    manager = MemoryManager(
        pg_session=db_session,
        qdrant=qdrant_client,
        redis=redis_client,
        embedder=DeterministicStubEmbedder(),
    )
    now = datetime.now(UTC)
    fact = SemanticFact(
        subject_id=node_id,
        predicate="lives_in",
        object_literal="Pune",
        valid_from=now,
        source_session=session_id,
        confidence=0.9,
    )

    [edge_first] = await manager.write_semantic([fact])
    [edge_second] = await manager.write_semantic([fact])

    assert edge_second.id == edge_first.id
    assert edge_second.valid_to is None  # still current, not superseded by itself


@pytest.mark.asyncio
async def test_query_semantic_returns_graph_fact_for_active_entity(
    db_session: AsyncSession, qdrant_client: AsyncQdrantClient, redis_client: Redis
) -> None:
    session_id, node_id = await _make_session_and_node(db_session)
    manager = MemoryManager(
        pg_session=db_session,
        qdrant=qdrant_client,
        redis=redis_client,
        embedder=DeterministicStubEmbedder(),
    )
    now = datetime.now(UTC)

    await manager.write_semantic(
        [
            SemanticFact(
                subject_id=node_id,
                predicate="prefers",
                object_literal="tea",
                valid_from=now,
                source_session=session_id,
                confidence=0.8,
            )
        ]
    )

    results = await manager.query(
        "what does Priya like",
        MemoryQuery(
            user_id=uuid.uuid4(),
            query_text="what does Priya like",
            top_k=5,
            active_entities=["Priya"],
        ),
    )
    assert any("prefers" in r.content and "tea" in r.content for r in results)


@pytest.mark.asyncio
async def test_refresh_and_get_warm_context_roundtrip(
    db_session: AsyncSession, qdrant_client: AsyncQdrantClient, redis_client: Redis
) -> None:
    session_id, _ = await _make_session_and_node(db_session)
    manager = MemoryManager(
        pg_session=db_session,
        qdrant=qdrant_client,
        redis=redis_client,
        embedder=DeterministicStubEmbedder(),
    )
    text = "Reminder: renew the domain before it expires."
    await manager.write_episodic(
        session_id, [EpisodicChunk(text=text, occurred_at=datetime.now(UTC))]
    )

    assert await manager.get_warm_context(session_id) is None  # cache miss before refresh

    state = SessionState(session_id=session_id, user_id=uuid.uuid4(), query_text=text, top_k=5)
    warm_slice = await manager.refresh_warm_context(state)
    assert warm_slice.session_id == session_id
    assert any(r.content == text for r in warm_slice.results)

    cached = await manager.get_warm_context(session_id)
    assert cached is not None
    assert cached.session_id == session_id
    assert [r.content for r in cached.results] == [r.content for r in warm_slice.results]
