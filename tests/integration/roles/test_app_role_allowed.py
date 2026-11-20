# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P6.1 / ADR-0026: the least-privilege role can still do every normal app
operation. A representative subset of the existing integration suites (memory,
approvals + ledger, scheduler, gateway ingest, flags) is re-run with the real
components over `roledb.app_sf`, which connects as the app role. The triggers
still fire for it (control at the end)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from src.approvals import service as svc
from src.gateway import UpdateStore
from src.gateway.store import Seen
from src.llm.pg_stores import PostgresFlagStore
from src.memory.schemas.ops import Job
from src.memory.store import ChunkIn
from src.scheduler.jobs import JobQueue
from tests._ledger_helpers import LedgerHarness, make_event
from tests._memory_helpers import make_event as contract_event
from tests._memory_helpers import query, store_for
from tests.fakes.clock import FakeClock
from tests.integration.roles.conftest import RoleDb

pytestmark = pytest.mark.asyncio


async def test_scheduler_job_lifecycle(roledb: RoleDb) -> None:
    clock = FakeClock()
    q = JobQueue(roledb.app_sf, clock)  # type: ignore[arg-type]
    key = f"k-{uuid.uuid4()}"
    jid, created = await q.enqueue("t", dedupe_key=key, payload={"a": 1})
    again, created2 = await q.enqueue("t", dedupe_key=key, payload={"a": 2})
    assert created and not created2 and jid == again
    got = await q.claim(kinds=frozenset({"t"}), limit=5)  # SELECT .. FOR UPDATE SKIP LOCKED
    mine = [j for j in got.jobs if j.dedupe_key == key]
    assert len(mine) == 1
    assert await q.complete(mine[0])
    await q.enqueue("t", dedupe_key=f"{key}-r", payload={}, rearm=True)  # ON CONFLICT DO UPDATE
    await q.enqueue("t", dedupe_key=f"{key}-r", payload={}, rearm=True)
    assert await q.cancel(f"{key}-r")
    clock.advance(minutes=1)
    await q.purge_finished(older_than=timedelta(seconds=1))  # the DELETE path
    async with roledb.app_sf() as s:
        n = await s.scalar(select(func.count()).select_from(Job).where(Job.dedupe_key == key))
    assert n in (0, 1)


async def test_flag_store_upsert_and_gateway_offsets(roledb: RoleDb) -> None:
    flags = PostgresFlagStore(roledb.app_sf)
    await flags.set("p61:k", {"v": 1}, updated_by="test")
    await flags.set("p61:k", {"v": 2}, updated_by="test")  # upsert (UPDATE on system_flag)
    assert await flags.get("p61:k") == {"v": 2}

    store = UpdateStore(roledb.app_sf)
    base = 8_100_000_000 + uuid.uuid4().int % 1_000_000
    assert await store.begin(base + 1, 1, "text") is Seen.NEW
    assert await store.begin(base + 1, 1, "text") is not Seen.NEW  # redelivery, idempotent
    await store.complete(base + 1)
    await store.record_dropped(base + 2, 5, "dropped")
    off = await store.get_offset()
    assert off is not None and off >= base + 3


async def test_memory_write_search_and_supersede(roledb: RoleDb) -> None:
    async with roledb.app_sf() as s, s.begin():
        store = store_for(s)
        eid = await store.record_event(contract_event(f"telegram:{uuid.uuid4().int % 10**9}"))
        ids = await store.add_chunks(eid, [ChunkIn(text="alpha chunk"), ChunkIn(text="beta chunk")])
        hits = await store.search(query("alpha chunk", top_k=3))
        assert any(h.chunk_id == ids[0] for h in hits)
        await store.invalidate(ids[0], superseded_by=ids[1])  # UPDATE on the allowed columns
    async with roledb.app_sf() as s:
        inv = await s.scalar(
            text("SELECT invalidated_at IS NOT NULL FROM memory_chunk WHERE id = :i"), {"i": ids[0]}
        )
    assert inv is True


async def test_approval_and_commitment_ledger_flow(roledb: RoleDb) -> None:
    lh = LedgerHarness(roledb.app_sf)
    ev = await make_event(lh.sf)
    (cand,) = await lh.intake.on_extracted(
        ev, [("send the budget deck to Priya", 0.9, "by Friday")]
    )
    card = lh.h.last_card()
    assert await lh.h.tap(card.approve_data) == "Approved. Running it now."  # pending -> consumed
    now = await lh.ledger.current(cand.key)
    assert now is not None and now.status == "confirmed"
    assert await lh.h.tap(card.approve_data) == svc.DONE_ALREADY  # single use


async def test_triggers_still_fire_for_the_app_role(roledb: RoleDb) -> None:
    """Control: the allowed UPDATE column set is not a blank cheque; the
    trigger layer is intact and active for this role."""
    async with roledb.app_sf() as s, s.begin():
        store = store_for(s)
        eid = await store.record_event(contract_event(f"telegram:{uuid.uuid4().int % 10**9}"))
        (cid,) = await store.add_chunks(eid, [ChunkIn(text="x")])
    async with roledb.app_sf() as s:
        with pytest.raises(DBAPIError):  # rewriting a set-once column
            await s.execute(
                text("UPDATE memory_chunk SET invalidated_at = now() WHERE id = :i"), {"i": cid}
            )
            await s.execute(
                text(
                    "UPDATE memory_chunk SET invalidated_at = now() + interval '1 day' "
                    "WHERE id = :i"
                ),
                {"i": cid},
            )
