# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.6 review queue on real Postgres with the real ApprovalService: list pending
untrusted chunks and edges, ask one card per item, approve promotes exactly that
item once, reject retracts it, and nothing is ever promoted without a tap."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.approvals import service as svc
from src.approvals.types import AskOutcome
from src.memory.graph import GraphRepository
from src.memory.review import ReviewQueue
from src.memory.schemas.graph import GraphNode
from src.memory.schemas.session import Session
from src.memory.store import ChunkIn
from tests._approval_helpers import Harness, build_harness, isolated_schema
from tests._memory_helpers import make_event, store_for

pytestmark = pytest.mark.asyncio

SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2  # pragma: allowlist secret


@pytest.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with isolated_schema() as (_, factory):
        yield factory


@pytest.fixture
def h(sf: async_sessionmaker[AsyncSession]) -> Harness:
    return build_harness(sf)


async def seed(sf: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    now = datetime.now(UTC)
    async with sf() as s, s.begin():
        store = store_for(s)
        bad = await store.record_event(make_event(lane="untrusted", kind="recorder_transcript"))  # type: ignore[arg-type]
        good = await store.record_event(make_event(lane="trusted"))
        (uc,) = await store.add_chunks(bad, [ChunkIn(text=f"forwarded claim {SECRET}")])
        (tc,) = await store.add_chunks(good, [ChunkIn(text="my own note")])
        sess = Session(user_id=uuid.uuid4(), mode="b", started_at=now, device_id="d")
        node = GraphNode(entity_type="person", canonical_name="Priya", created_at=now)
        s.add_all([sess, node])
        await s.flush()
        edge = await GraphRepository(s).insert_fact(
            subject_id=node.id,
            predicate="works_at",
            object_id=None,
            object_literal="Evil Corp",
            valid_from=now,
            source_session=sess.id,
            confidence=0.7,
            lane="untrusted",
            source_event_id=bad,
        )
    return {"uchunk": uc, "tchunk": tc, "uedge": edge.id}


async def flags(h: Harness, table: str, item: uuid.UUID) -> tuple[bool, bool]:
    (row,) = await h.rows(
        f"SELECT promoted, invalidated_at IS NOT NULL FROM {table} WHERE id = :i", i=item
    )
    return bool(row[0]), bool(row[1])


async def test_list_pending_shows_only_untrusted_unpromoted_scrubbed(
    sf: async_sessionmaker[AsyncSession], h: Harness
) -> None:
    ids = await seed(sf)
    q = ReviewQueue(sf, h.service)
    items = await q.list_pending()
    assert {i.item_id for i in items} == {ids["uchunk"], ids["uedge"]}
    assert {i.kind for i in items} == {"chunk", "edge"}
    assert all(SECRET not in i.preview for i in items)
    assert await q.pending_count() == 2


async def test_drain_asks_but_never_promotes_and_is_bounded(
    sf: async_sessionmaker[AsyncSession], h: Harness
) -> None:
    ids = await seed(sf)
    q = ReviewQueue(sf, h.service)
    assert await q.drain(max_cards=1) == 1
    assert len(h.gw.cards) == 1
    assert await q.drain(max_cards=5) == 1  # first is a duplicate-open, second is sent
    assert len(h.gw.cards) == 2
    assert await flags(h, "memory_chunk", ids["uchunk"]) == (False, False)
    assert await flags(h, "graph_edge", ids["uedge"]) == (False, False)


async def test_approve_card_promotes_chunk_and_edge_once(
    sf: async_sessionmaker[AsyncSession], h: Harness
) -> None:
    ids = await seed(sf)
    q = ReviewQueue(sf, h.service)
    h.service.bind_promoter(q.promote)
    for kind, table in (("uchunk", "memory_chunk"), ("uedge", "graph_edge")):
        assert await h.service.request_memory_promotion(ids[kind]) is AskOutcome.REQUESTED
        card = h.last_card()
        await h.tap(card.approve_data)
        assert await flags(h, table, ids[kind]) == (True, False)
        assert await h.tap(card.approve_data) == svc.DONE_ALREADY
    assert await q.pending_count() == 0


async def test_reject_card_retracts_the_item_and_leaves_trusted_alone(
    sf: async_sessionmaker[AsyncSession], h: Harness
) -> None:
    from src.app.runtime import memory_review_listener

    ids = await seed(sf)
    q = ReviewQueue(sf, h.service)
    h.service.bind_promoter(q.promote)
    h.service.bind_listener(memory_review_listener(q))
    await h.service.request_memory_promotion(ids["uedge"])
    await h.tap(h.last_card().reject_data)
    assert await flags(h, "graph_edge", ids["uedge"]) == (False, True)
    assert await q.pending_count() == 1
    assert await q.reject_via_card(ids["tchunk"]) is False  # trusted rows are never touched
    assert await flags(h, "memory_chunk", ids["tchunk"]) == (True, False)


async def test_promoter_refuses_without_a_matching_consumed_approval(
    sf: async_sessionmaker[AsyncSession], h: Harness
) -> None:
    from src.memory.store import ApprovalRequiredError, ChunkNotFoundError

    ids = await seed(sf)
    q = ReviewQueue(sf, h.service)
    with pytest.raises(ApprovalRequiredError):
        await q.promote(ids["uchunk"], uuid.uuid4())
    with pytest.raises(ChunkNotFoundError):
        await q.promote(uuid.uuid4(), uuid.uuid4())
    assert await q.pending_count() == 2


async def test_one_card_cannot_promote_a_second_item(
    sf: async_sessionmaker[AsyncSession], h: Harness
) -> None:
    from src.memory.store import ApprovalRequiredError

    ids = await seed(sf)
    q = ReviewQueue(sf, h.service)
    h.service.bind_promoter(q.promote)
    await h.service.request_memory_promotion(ids["uchunk"])
    await h.tap(h.last_card().approve_data)
    aid = await h.only_action_id()
    with pytest.raises(ApprovalRequiredError):
        await q.promote(ids["uedge"], aid)
    assert await flags(h, "graph_edge", ids["uedge"]) == (False, False)
    (n,) = await h.rows("SELECT count(*) FROM pending_action")
    assert n[0] == 1
