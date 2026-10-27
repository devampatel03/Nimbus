# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""MEM-3: contradiction detection is a SQL predicate — same subject_id,
same predicate, different object, overlapping valid-time.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.graph import GraphRepository
from src.memory.schemas.graph import GraphNode
from src.memory.schemas.session import Session


async def _make_session_and_subject(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    session_row = Session(
        user_id=uuid.uuid4(), mode="mode_b", started_at=now, device_id="test-device"
    )
    subject = GraphNode(entity_type="person", canonical_name="Devam", created_at=now)
    db_session.add_all([session_row, subject])
    await db_session.flush()
    return session_row.id, subject.id


@pytest.mark.asyncio
async def test_overlapping_facts_with_different_objects_are_flagged(db_session: AsyncSession):
    session_id, subject_id = await _make_session_and_subject(db_session)
    repo = GraphRepository(db_session)
    now = datetime.now(UTC)

    edge_a = await repo.insert_fact(
        subject_id=subject_id,
        predicate="works_at",
        object_id=None,
        object_literal="Acme Corp",
        valid_from=now - timedelta(days=1),
        source_session=session_id,
        confidence=0.9,
        lane="trusted",
    )
    edge_b = await repo.insert_fact(
        subject_id=subject_id,
        predicate="works_at",
        object_id=None,
        object_literal="Globex Inc",
        valid_from=now,
        source_session=session_id,
        confidence=0.85,
        lane="trusted",
    )

    pairs = await repo.find_contradictions(subject_id, "works_at")

    # `find_contradictions` dedupes pairs via `a.id < b.id` (UUID order),
    # not insertion order, so check the pair as an unordered set.
    found_pairs = {frozenset((pair[0].id, pair[1].id)) for pair in pairs}
    assert frozenset((edge_a.id, edge_b.id)) in found_pairs


@pytest.mark.asyncio
async def test_non_overlapping_facts_are_not_flagged(db_session: AsyncSession):
    session_id, subject_id = await _make_session_and_subject(db_session)
    repo = GraphRepository(db_session)
    now = datetime.now(UTC)

    old_edge = await repo.insert_fact(
        subject_id=subject_id,
        predicate="works_at",
        object_id=None,
        object_literal="Acme Corp",
        valid_from=now - timedelta(days=30),
        source_session=session_id,
        confidence=0.9,
        lane="trusted",
    )
    # Properly closed out before the new fact starts: no overlap.
    old_edge.valid_to = now - timedelta(days=1)
    await db_session.flush()

    await repo.insert_fact(
        subject_id=subject_id,
        predicate="works_at",
        object_id=None,
        object_literal="Globex Inc",
        valid_from=now,
        source_session=session_id,
        confidence=0.85,
        lane="trusted",
    )

    pairs = await repo.find_contradictions(subject_id, "works_at")
    assert pairs == []


@pytest.mark.asyncio
async def test_same_object_is_not_a_contradiction(db_session: AsyncSession):
    session_id, subject_id = await _make_session_and_subject(db_session)
    repo = GraphRepository(db_session)
    now = datetime.now(UTC)

    await repo.insert_fact(
        subject_id=subject_id,
        predicate="works_at",
        object_id=None,
        object_literal="Acme Corp",
        valid_from=now - timedelta(days=1),
        source_session=session_id,
        confidence=0.9,
        lane="trusted",
    )
    await repo.insert_fact(
        subject_id=subject_id,
        predicate="works_at",
        object_id=None,
        object_literal="Acme Corp",
        valid_from=now,
        source_session=session_id,
        confidence=0.95,
        lane="trusted",
    )

    pairs = await repo.find_contradictions(subject_id, "works_at")
    assert pairs == []
