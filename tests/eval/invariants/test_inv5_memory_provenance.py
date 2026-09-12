# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-5: Every derived memory row has non-null provenance.

Acceptance: any row in `graph_edge`, `commitment` or `decision` — the
tables that carry a `source_session` provenance column — must have a
non-null `source_session`, and the database must reject attempts to
insert one with a null value (schema-level enforcement, not just current
absence of bad rows).

Against a real Postgres instance (docker compose up -d postgres), per
AGENT_OPERATING_MANUAL.md's real-dependency requirement.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.schemas.commitment import Commitment
from src.memory.schemas.decision import Decision
from src.memory.schemas.graph import GraphEdge, GraphNode
from src.memory.schemas.session import Session

PROVENANCE_TABLES = (GraphEdge, Commitment, Decision)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", PROVENANCE_TABLES)
async def test_no_existing_rows_have_null_source_session(db_session: AsyncSession, model):
    result = await db_session.execute(
        select(func.count()).select_from(model).where(model.source_session.is_(None))
    )
    assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_graph_edge_rejects_null_source_session(db_session: AsyncSession):
    now = datetime.now(UTC)
    subject = GraphNode(entity_type="person", canonical_name="Devam", created_at=now)
    db_session.add(subject)
    await db_session.flush()

    bad_edge = GraphEdge(
        subject_id=subject.id,
        predicate="lives_in",
        object_literal="Mumbai",
        valid_from=now,
        ingested_at=now,
        source_session=None,  # type: ignore[arg-type]
        confidence=0.9,
    )
    db_session.add(bad_edge)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_commitment_rejects_null_source_session(db_session: AsyncSession):
    now = datetime.now(UTC)
    bad_commitment = Commitment(
        source_session=None,  # type: ignore[arg-type]
        description="Follow up with Alex",
        status="open",
        confidence=0.8,
        created_at=now,
    )
    db_session.add(bad_commitment)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_decision_rejects_null_source_session(db_session: AsyncSession):
    now = datetime.now(UTC)
    bad_decision = Decision(
        source_session=None,  # type: ignore[arg-type]
        decision_text="Switch to Postgres+AGE",
        reasoning="Single store for graph and relational data",
        created_at=now,
        review_at=now,
    )
    db_session.add(bad_decision)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_valid_source_session_is_accepted(db_session: AsyncSession):
    now = datetime.now(UTC)
    session_row = Session(
        user_id=uuid.uuid4(), mode="mode_b", started_at=now, device_id="test-device"
    )
    db_session.add(session_row)
    await db_session.flush()

    decision = Decision(
        source_session=session_row.id,
        decision_text="Switch to Postgres+AGE",
        reasoning="Single store for graph and relational data",
        created_at=now,
        review_at=now,
    )
    db_session.add(decision)
    await db_session.flush()
    assert decision.source_session == session_row.id
