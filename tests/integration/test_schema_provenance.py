# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""provenance_coverage (threshold memory.provenance_coverage = 1.0): rows
created through every row-creating path leave NO NULL provenance. Paths
exercised: GraphRepository.insert_fact/supersede (what MemoryManager
.write_semantic delegates to), and ORM creation of Event, MemoryChunk,
Commitment, Decision. Runs on the ORM-created (create_all) schema, which
carries the same triggers/constraints as the migration.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.graph import GraphRepository
from src.memory.schemas import (
    Commitment,
    Decision,
    Event,
    GraphNode,
    MemoryChunk,
    Session,
)

# table -> columns that together are the row's provenance
PROVENANCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "event": ("source", "source_key", "lane", "confidence", "created_at"),
    "memory_chunk": ("source_event_id", "lane", "confidence", "created_at"),
    "graph_edge": ("source_session", "lane", "confidence", "ingested_at"),
    "commitment": ("source_session", "lane", "confidence", "created_at"),
    "decision": ("source_session", "lane", "confidence", "created_at"),
}


async def provenance_coverage(session: AsyncSession) -> tuple[float, int]:
    """(fraction of derived rows with all provenance columns non-null, row count)."""
    total = complete = 0
    for table, cols in PROVENANCE_COLUMNS.items():
        all_set = " AND ".join(f"{c} IS NOT NULL" for c in cols)
        row = (
            await session.execute(
                text(f"SELECT count(*), count(*) FILTER (WHERE {all_set}) FROM {table}")
            )
        ).one()
        total += row[0]
        complete += row[1]
    return (complete / total if total else 1.0), total


async def test_provenance_coverage_is_one_across_all_row_creating_paths(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    sess = Session(user_id=uuid.uuid4(), mode="b", started_at=now, device_id="d")
    event = Event(
        source_key=f"tg:update:{uuid.uuid4()}",
        kind="text",
        source="telegram",
        lane="trusted",
        occurred_at=now,
    )
    node = GraphNode(entity_type="person", canonical_name="Devam", created_at=now)
    db_session.add_all([sess, event, node])
    await db_session.flush()

    repo = GraphRepository(db_session)
    edge = await repo.insert_fact(
        subject_id=node.id,
        predicate="lives_in",
        object_id=None,
        object_literal="Pune",
        valid_from=now,
        source_session=sess.id,
        confidence=0.9,
        lane="trusted",
    )
    await repo.supersede(
        edge,
        object_id=None,
        object_literal="Mumbai",
        valid_from=now,
        confidence=0.8,
        lane="trusted",
    )
    db_session.add_all(
        [
            MemoryChunk(
                source_event_id=event.id,
                content="hello",
                embedding=[0.1] * 1024,
                model_id="qwen3-0.6b",
                lane="trusted",
            ),
            MemoryChunk(
                source_event_id=event.id,
                chunk_index=1,
                content="from a web page",
                embedding=[0.2] * 1024,
                model_id="qwen3-0.6b",
                lane="untrusted",
                promoted=False,
            ),
            Commitment(
                source_session=sess.id,
                description="call mom",
                confidence=0.8,
                created_at=now,
                lane="trusted",
            ),
            Decision(
                source_session=sess.id,
                decision_text="use pgvector",
                reasoning="one store",
                created_at=now,
                review_at=now,
                lane="trusted",
            ),
        ]
    )
    await db_session.flush()

    coverage, total = await provenance_coverage(db_session)
    assert total >= 7  # event, 2 edges, 2 chunks, commitment, decision
    assert coverage == 1.0


async def test_coverage_metric_detects_a_gap(db_session: AsyncSession) -> None:
    """Prove the metric can fail: drop NOT NULL inside the (rolled-back) test
    transaction, insert an orphan chunk, and the coverage must drop below 1."""
    now = datetime.now(UTC)
    event = Event(
        source_key=f"tg:update:{uuid.uuid4()}",
        kind="text",
        source="telegram",
        lane="trusted",
        occurred_at=now,
    )
    db_session.add(event)
    await db_session.flush()
    await db_session.execute(
        text("ALTER TABLE memory_chunk ALTER COLUMN source_event_id DROP NOT NULL")
    )
    await db_session.execute(
        text(
            "INSERT INTO memory_chunk (content, embedding, model_id, lane) "
            "VALUES ('orphan', CAST(:v AS vector), 'm', 'trusted')"
        ),
        {"v": "[" + ",".join(["0.3"] * 1024) + "]"},
    )
    coverage, _ = await provenance_coverage(db_session)
    assert coverage < 1.0
