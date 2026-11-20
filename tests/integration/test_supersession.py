# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""INV-3 / T2.9: insert a fact, supersede it, and verify the old row is
untouched (object columns, valid_from) while a new row exists carrying the
new value and pointing back via `superseded_by`.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.graph import GraphRepository
from src.memory.schemas.graph import GraphNode
from src.memory.schemas.person import Person
from src.memory.schemas.session import Session


async def _make_session_and_nodes(
    db_session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    session_row = Session(
        user_id=uuid.uuid4(), mode="mode_b", started_at=now, device_id="test-device"
    )
    subject = GraphNode(entity_type="person", canonical_name="Devam", created_at=now)
    person = Person(display_name="Devam")
    db_session.add_all([session_row, subject, person])
    await db_session.flush()
    return session_row.id, subject.id, person.id


@pytest.mark.asyncio
async def test_updating_a_fact_creates_supersession_not_mutation(db_session: AsyncSession):
    session_id, subject_id, person_id = await _make_session_and_nodes(db_session)
    repo = GraphRepository(db_session)
    now = datetime.now(UTC)

    old_edge = await repo.insert_fact(
        subject_id=subject_id,
        predicate="lives_in",
        object_id=None,
        object_literal="Mumbai",
        valid_from=now,
        source_session=session_id,
        confidence=0.9,
        source_speaker=person_id,
        lane="trusted",
    )
    original_object_literal = old_edge.object_literal
    original_valid_from = old_edge.valid_from
    original_id = old_edge.id

    new_edge = await repo.supersede(
        old_edge,
        object_id=None,
        object_literal="Bengaluru",
        valid_from=now,
        confidence=0.95,
        lane="trusted",
    )

    # Old row: object columns and valid_from untouched, but now closed out.
    refreshed_old = await db_session.get(type(old_edge), original_id)
    assert refreshed_old is not None
    assert refreshed_old.object_literal == original_object_literal
    assert refreshed_old.valid_from == original_valid_from
    assert refreshed_old.valid_to is not None
    assert refreshed_old.superseded_by == new_edge.id

    # New row: a distinct fact carrying the update.
    assert new_edge.id != original_id
    assert new_edge.object_literal == "Bengaluru"
    assert new_edge.subject_id == subject_id
    assert new_edge.predicate == "lives_in"
    assert new_edge.valid_to is None
    assert new_edge.superseded_by is None

    result = await db_session.execute(
        select(type(old_edge)).where(type(old_edge).subject_id == subject_id)
    )
    rows = result.scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_lane", "new_lane"), [("trusted", "untrusted"), ("untrusted", "trusted")]
)
async def test_supersede_across_lanes_is_refused_and_changes_nothing(
    db_session: AsyncSession, old_lane: str, new_lane: str
) -> None:
    from src.memory.graph import CrossLaneSupersedeError

    session_id, subject_id, _ = await _make_session_and_nodes(db_session)
    repo = GraphRepository(db_session)
    old_edge = await repo.insert_fact(
        subject_id=subject_id,
        predicate="lives_in",
        object_id=None,
        object_literal="Mumbai",
        valid_from=datetime.now(UTC),
        source_session=session_id,
        confidence=0.9,
        lane=old_lane,  # type: ignore[arg-type]
    )
    with pytest.raises(CrossLaneSupersedeError):
        await repo.supersede(
            old_edge,
            object_id=None,
            object_literal="Delhi",
            valid_from=datetime.now(UTC),
            confidence=0.9,
            lane=new_lane,  # type: ignore[arg-type]
        )
    assert old_edge.valid_to is None and old_edge.superseded_by is None
