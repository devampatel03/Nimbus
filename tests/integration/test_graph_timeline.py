# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P2.4 round 1 (H2/M3): graph reads over (as_of, known_at).

`known_at` answers "what did the system believe then": an edge's `valid_to`
only counts if its successor had been *recorded* by then. A back-dated
correction (valid_from earlier than its ingest time) therefore must not
retroactively close the old fact for earlier record times."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.graph import GraphRepository
from src.memory.manager import MemoryManager
from src.memory.schemas.graph import GraphEdge, GraphNode
from src.memory.schemas.session import Session
from tests._memory_helpers import EMB, query

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def d(n: float) -> datetime:
    return T0 + timedelta(days=n)


async def world(db: AsyncSession, name: str) -> tuple[GraphNode, Session]:
    sess = Session(user_id=uuid.uuid4(), mode="mode_b", started_at=T0, device_id="d")
    node = GraphNode(entity_type="person", canonical_name=name, created_at=T0)
    db.add_all([sess, node])
    await db.flush()
    return node, sess


async def edge(
    db: AsyncSession, node: GraphNode, sess: Session, obj: str, *, vf: datetime, ing: datetime
) -> GraphEdge:
    e = GraphEdge(
        subject_id=node.id,
        predicate="lives_in",
        object_literal=obj,
        valid_from=vf,
        ingested_at=ing,
        source_session=sess.id,
        confidence=0.9,
    )
    db.add(e)
    await db.flush()
    return e


async def link(db: AsyncSession, old: GraphEdge, new: GraphEdge) -> None:
    """What `supersede` does, with controlled timestamps."""
    await db.execute(
        text("UPDATE graph_edge SET valid_to = :vt, superseded_by = :n WHERE id = :o"),
        {"vt": new.valid_from, "n": new.id, "o": old.id},
    )


async def lives(mgr: MemoryManager, name: str, **kw: Any) -> set[str]:
    res = await mgr.query("w", query("w", active_entities=[name], **kw))
    return {r.content.split()[-1] for r in res}


# (as_of_day, known_at_day, expected); None = unconstrained
BACKDATED: list[tuple[int | None, int | None, set[str]]] = [
    (None, None, {"Mumbai"}),  # now: the correction is known
    (5, None, {"Mumbai"}),  # valid day5, current knowledge
    (2, None, {"Pune"}),
    (None, 5, {"Pune"}),  # on day5 only Pune was recorded (H2)
    (5, 5, {"Pune"}),
    (2, 5, {"Pune"}),
    (None, 10, {"Mumbai"}),  # equal timestamps: ingested_at == known_at counts as known
    (5, 10, {"Mumbai"}),
    (2, 10, {"Pune"}),
    (None, 1, {"Pune"}),  # equal to Pune's own ingest
    (None, 0, set()),  # nothing recorded yet
    (0, None, set()),  # nothing valid yet
]


@pytest.mark.parametrize(("as_of", "known", "expected"), BACKDATED)
async def test_back_dated_correction(
    db_session: AsyncSession, as_of: int | None, known: int | None, expected: set[str]
) -> None:
    node, sess = await world(db_session, "BackDated")
    pune = await edge(db_session, node, sess, "Pune", vf=d(1), ing=d(1))
    mumbai = await edge(db_session, node, sess, "Mumbai", vf=d(3), ing=d(10))
    await link(db_session, pune, mumbai)
    mgr = MemoryManager(pg_session=db_session, embedder=EMB)
    kw: dict[str, Any] = {}
    if as_of is not None:
        kw["as_of"] = d(as_of)
    if known is not None:
        kw["known_at"] = d(known)
    assert await lives(mgr, "BackDated", **kw) == expected


async def test_derived_valid_to_is_reported_as_known_at_known_at(
    db_session: AsyncSession,
) -> None:
    node, sess = await world(db_session, "ValidTo")
    pune = await edge(db_session, node, sess, "Pune", vf=d(1), ing=d(1))
    mumbai = await edge(db_session, node, sess, "Mumbai", vf=d(3), ing=d(10))
    await link(db_session, pune, mumbai)
    mgr = MemoryManager(pg_session=db_session, embedder=EMB)
    early = await mgr.query("w", query("w", active_entities=["ValidTo"], known_at=d(5)))
    assert [(r.content.split()[-1], r.valid_to) for r in early] == [("Pune", None)]
    late = await mgr.query("w", query("w", active_entities=["ValidTo"], as_of=d(2), known_at=d(10)))
    assert [(r.content.split()[-1], r.valid_to) for r in late] == [("Pune", d(3))]


FUTURE = [
    (10, 5, {"Pune"}),  # successor known (day2) but not yet valid
    (25, 5, {"Mumbai"}),
    (20, 5, {"Mumbai"}),  # equal timestamps: valid_from inclusive, valid_to exclusive
    (25, 1, {"Pune"}),  # successor not yet recorded on day1: Pune open-ended
    (25, 2, {"Mumbai"}),  # equal to the successor's ingest
]


@pytest.mark.parametrize(("as_of", "known", "expected"), FUTURE)
async def test_future_dated_successor(
    db_session: AsyncSession, as_of: int, known: int, expected: set[str]
) -> None:
    node, sess = await world(db_session, "Future")
    pune = await edge(db_session, node, sess, "Pune", vf=d(1), ing=d(1))
    mumbai = await edge(db_session, node, sess, "Mumbai", vf=d(20), ing=d(2))
    await link(db_session, pune, mumbai)
    mgr = MemoryManager(pg_session=db_session, embedder=EMB)
    assert await lives(mgr, "Future", as_of=d(as_of), known_at=d(known)) == expected


async def test_record_time_invalidation_is_respected_per_known_at(
    db_session: AsyncSession,
) -> None:
    node, sess = await world(db_session, "Retracted")
    e = await edge(db_session, node, sess, "Pune", vf=d(1), ing=d(2))
    await db_session.execute(
        text("UPDATE graph_edge SET invalidated_at = :t WHERE id = :i"), {"t": d(6), "i": e.id}
    )
    mgr = MemoryManager(pg_session=db_session, embedder=EMB)
    assert await lives(mgr, "Retracted", known_at=d(1)) == set()  # not yet recorded
    assert await lives(mgr, "Retracted", known_at=d(3)) == {"Pune"}  # believed
    assert await lives(mgr, "Retracted", known_at=d(6)) == set()  # retracted at exactly 6
    assert await lives(mgr, "Retracted") == set()  # now


async def test_current_read_uses_now_not_just_open_valid_to(db_session: AsyncSession) -> None:
    """M3: a future-dated successor must not displace the fact valid today."""
    node, sess = await world(db_session, "Soon")
    now = datetime.now(UTC)
    repo = GraphRepository(db_session)
    a = await repo.insert_fact(
        subject_id=node.id,
        predicate="lives_in",
        object_id=None,
        object_literal="Pune",
        valid_from=now - timedelta(days=100),
        source_session=sess.id,
        confidence=0.9,
    )
    await repo.supersede(
        a,
        object_id=None,
        object_literal="Mumbai",
        valid_from=now + timedelta(days=30),
        confidence=0.9,
    )
    mgr = MemoryManager(pg_session=db_session, embedder=EMB)
    assert await lives(mgr, "Soon") == {"Pune"}
    assert await lives(mgr, "Soon", as_of=now + timedelta(days=31)) == {"Mumbai"}


async def test_supersede_with_earlier_valid_from_yields_empty_interval(
    db_session: AsyncSession,
) -> None:
    """M2/ADR-0017: new.valid_from < old.valid_from => the old fact's interval is
    zero-length (valid_to == valid_from), never negative; it is never 'valid'."""
    node, sess = await world(db_session, "Zero")
    repo = GraphRepository(db_session)
    a = await repo.insert_fact(
        subject_id=node.id,
        predicate="p",
        object_id=None,
        object_literal="A",
        valid_from=d(5),
        source_session=sess.id,
        confidence=0.9,
    )
    b = await repo.supersede(a, object_id=None, object_literal="B", valid_from=d(2), confidence=0.9)
    assert a.valid_to == a.valid_from == d(5)
    assert a.superseded_by == b.id
    mgr = MemoryManager(pg_session=db_session, embedder=EMB)
    assert await lives(mgr, "Zero", as_of=d(5)) == {"B"}
    assert await lives(mgr, "Zero", as_of=d(1)) == set()


async def test_supersede_keeps_t29_semantics_for_forward_supersession(
    db_session: AsyncSession,
) -> None:
    node, sess = await world(db_session, "Fwd")
    repo = GraphRepository(db_session)
    a = await repo.insert_fact(
        subject_id=node.id,
        predicate="p",
        object_id=None,
        object_literal="A",
        valid_from=d(1),
        source_session=sess.id,
        confidence=0.9,
    )
    b = await repo.supersede(a, object_id=None, object_literal="B", valid_from=d(4), confidence=0.9)
    assert (a.valid_to, a.object_literal, a.valid_from) == (d(4), "A", d(1))
    assert b.valid_to is None
