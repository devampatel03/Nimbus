# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`PgEventSink` against real Postgres (P1.2c `event` table): the UNIQUE
`source_key` makes redelivery a no-op, and pending follow-ons are found."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from src.contracts.event import Event, EventKind
from src.ingest import Ingestor, PgEventSink
from src.memory.schemas.event import Event as Row
from tests.unit.ingest.fakes import (
    CLK,
    NOW,
    FakeGateway,
    FakeTranscriber,
    clock,
    text_update,
    voice_update,
)


@pytest.fixture
async def sf(pg_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    conn: AsyncConnection
    async with pg_engine.connect() as conn:
        tx = await conn.begin()
        yield async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        await tx.rollback()


async def test_same_update_twice_one_row(sf: async_sessionmaker[AsyncSession]) -> None:
    calls: list[str] = []
    ing = Ingestor(PgEventSink(sf), FakeGateway(calls), FakeTranscriber(calls), clock=clock)
    a = await ing.store(text_update(4242, "hello\x00 world"))
    b = await ing.store(text_update(4242, "hello world"))
    assert a and b and a.event_id == b.event_id and a.created and not b.created
    async with sf() as s:
        row = (await s.execute(select(Row).where(Row.source_key == "telegram:4242"))).scalar_one()
        n = await s.scalar(
            select(func.count()).select_from(Row).where(Row.source_key == "telegram:4242")
        )
    assert (row.kind, row.lane, row.body) == ("text", "trusted", "hello world")
    assert n == 1


async def test_voice_follow_on_and_recovery_in_postgres(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []
    gw = FakeGateway(calls, files={"fid8801": b"OggS"})
    ing = Ingestor(PgEventSink(sf), gw, FakeTranscriber(calls), clock=clock)
    assert await ing.store(voice_update(8801, untrusted=True))
    sink = PgEventSink(sf)
    pending = await sink.pending_follow_ons(10, **_win())
    mine = [p for p in pending if p.meta.get("update_id") == 8801]
    assert len(mine) == 1 and mine[0].lane == "untrusted"
    CLK.now = NOW
    CLK.advance(601)
    try:
        await ing.recover_pending(limit=1000)
    finally:
        CLK.now = NOW
    assert not [
        p for p in await sink.pending_follow_ons(1000, **_win()) if p.meta.get("update_id") == 8801
    ]
    async with sf() as s:
        row = (
            await s.execute(select(Row).where(Row.source_key == "telegram:8801:transcript"))
        ).scalar_one()
    assert row.kind == "voice_transcript" and row.lane == "untrusted"


# -- hostile Telegram metadata through real Postgres (MED-1) ---------------


async def test_hostile_metadata_does_not_crash_run_or_poison_update(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    from src.gateway.types import FileRef, InboundUpdate, UpdateKind

    calls: list[str] = []
    evil_mime = "audio/\x00ogg\ud800" + "x" * 2_000_000
    ref = FileRef("fid7001", "uniq7001", 10, evil_mime, "a\x00b\ud800.txt", UpdateKind.VOICE)
    ups = [
        InboundUpdate(7001, UpdateKind.VOICE, message_id=1, file=ref),
        InboundUpdate(7002, UpdateKind.TEXT, message_id=2, text="after the hostile one"),
    ]
    gw = FakeGateway(calls, ups, {"fid7001": b"OggS"})
    ing = Ingestor(PgEventSink(sf), gw, FakeTranscriber(calls), clock=clock)
    await ing.run()
    assert [a[0] for a in gw.acked] == [7001, 7002]
    async with sf() as s:
        row = (await s.execute(select(Row).where(Row.source_key == "telegram:7002"))).scalar_one()
    assert row.body == "after the hostile one"


# -- DB errors map to a fixed code (MED-2, INV-7) ---------------------------


async def test_db_error_is_a_fixed_code_without_parameters(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from src.contracts.event import Event, EventKind
    from src.ingest.protocols import EventStoreError

    canary = "USER-PRIVATE-TEXT"
    ev = Event(
        source_key="telegram:7100",
        kind=EventKind.TEXT,
        source="telegram",
        lane="trusted",
        body=canary + "\x00",  # NUL: Postgres rejects it
        occurred_at=datetime.now(UTC),
    )
    with pytest.raises(EventStoreError) as ei:
        await PgEventSink(sf).record(ev)
    assert str(ei.value) == "event_store_failed"
    assert canary not in repr(ei.value) and canary not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


async def test_pending_excludes_unrecoverable_and_tombstoned_and_respects_age(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime, timedelta

    from src.contracts.event import Event, EventKind

    sink = PgEventSink(sf)
    t0 = datetime.now(UTC) - timedelta(hours=1)

    def ev(i: int, **kw: object) -> Event:
        base = {
            "source_key": f"telegram:{i}",
            "kind": EventKind.VOICE,
            "source": "telegram",
            "lane": "trusted",
            "meta": {"update_id": i, "file_id": f"f{i}"},
            "file_unique_id": f"u{i}",
            "occurred_at": t0,
        }
        base.update(kw)
        return Event(**base)  # type: ignore[arg-type]

    await sink.record(ev(7201))  # recoverable
    await sink.record(ev(7202, meta={"update_id": 7202}))  # no file_id
    await sink.record(ev(7203))
    await sink.record(  # tombstone for 7203
        Event(
            source_key="telegram:7203:transcript",
            kind=EventKind.VOICE_TRANSCRIPT,
            source="telegram",
            lane="untrusted",
            confidence=0.0,
            meta={"rejected": "x"},
            occurred_at=t0,
        )
    )
    await sink.record(ev(7204, occurred_at=datetime.now(UTC)))  # too young
    cut = datetime.now(UTC)
    got = await sink.pending_follow_ons(
        100, older_than=cut - timedelta(minutes=10), now=cut, backoff_s=BACKOFF
    )
    ids = {p.meta["update_id"] for p in got}
    assert 7201 in ids and not ids & {7202, 7203, 7204}
    pid = next(p.id for p in got if p.meta["update_id"] == 7201)
    assert await sink.bump_attempts(pid, now=cut, backoff_s=BACKOFF) == 1
    # claimed: second claim inside the backoff window is refused (two instances)
    assert await sink.bump_attempts(pid, now=cut, backoff_s=BACKOFF) is None
    later = cut + timedelta(seconds=BACKOFF[0] + 1)
    assert await sink.bump_attempts(pid, now=later, backoff_s=BACKOFF) == 2


BACKOFF = (600.0, 3600.0, 21_600.0)


def _win() -> dict[str, object]:
    far = NOW + timedelta(days=1)
    return {"older_than": far, "now": far, "backoff_s": BACKOFF}


async def _seed(sink: PgEventSink, i: int, when: datetime) -> uuid.UUID:
    eid, _ = await sink.record(
        Event(
            source_key=f"telegram:{i}",
            kind=EventKind.VOICE,
            source="telegram",
            lane="trusted",
            meta={"update_id": i, "file_id": f"f{i}"},
            file_unique_id=f"u{i}",
            occurred_at=when,
        )
    )
    return eid


async def test_pending_orders_by_attempts_before_age(sf: async_sessionmaker[AsyncSession]) -> None:
    """Kills the 'order by occurred_at only' mutant: an old, repeatedly failing
    event must not take the single window slot from a newer untried one."""
    sink = PgEventSink(sf)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    old = await _seed(sink, 7301, t0)
    await _seed(sink, 7302, t0 + timedelta(hours=1))
    await sink.bump_attempts(old, now=t0, backoff_s=BACKOFF)
    now = t0 + timedelta(days=2)
    got = await sink.pending_follow_ons(1, older_than=now, now=now, backoff_s=BACKOFF)
    assert [p.meta["update_id"] for p in got] == [7302]


async def test_pending_respects_backoff_and_clear_removes_counter(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    sink = PgEventSink(sf)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    eid = await _seed(sink, 7311, t0)
    await sink.bump_attempts(eid, now=t0, backoff_s=BACKOFF)
    soon = t0 + timedelta(minutes=5)
    assert not await sink.pending_follow_ons(10, older_than=soon, now=soon, backoff_s=BACKOFF)
    due = t0 + timedelta(minutes=11)
    got = await sink.pending_follow_ons(10, older_than=due, now=due, backoff_s=BACKOFF)
    assert [p.attempts for p in got] == [1]
    await sink.clear_attempts(eid)
    got = await sink.pending_follow_ons(10, older_than=soon, now=soon, backoff_s=BACKOFF)
    assert [p.attempts for p in got] == [0]
