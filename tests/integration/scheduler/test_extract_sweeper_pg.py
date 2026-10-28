# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The scheduler tick that backstops the post-turn enqueue: every trusted user
text/voice-transcript event gets exactly one `memory_extract` job, including the
ones whose turn crashed before the enqueue. Untrusted events never do. After
downtime the backlog is enqueued at no more than 10 jobs a minute (R5), and a job
that died of a transient cause can be enqueued again."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.ops import Job
from src.scheduler.jobs import JobQueue
from src.scheduler.memory_extract import KIND, MemoryExtractSweeper
from tests.fakes.clock import FakeClock

SF = async_sessionmaker[AsyncSession]


def base(n: int) -> datetime:
    """Each test lives in its own time window: events are append-only, so tests
    cannot clean up, and the sweeper only looks back two days."""
    return datetime(2031, 1, 1, tzinfo=UTC) + timedelta(days=10 * n)


BODY = "remember this for later, please: " + "x" * 20


async def ev(
    sf: SF, *, lane: str, at: datetime, kind: str = "text", body: str | None = BODY
) -> uuid.UUID:
    async with sf() as s, s.begin():
        e = EventRow(
            source_key=f"sw:{uuid.uuid4()}",
            kind=kind,
            source="telegram",
            lane=lane,
            body=body,
            meta={},
            occurred_at=at,
            created_at=at,
        )
        s.add(e)
        await s.flush()
        return e.id


async def jobs_for(sf: SF, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    async with sf() as s:
        rows = await s.scalars(
            select(Job.source_event_id).where(Job.kind == KIND, Job.source_event_id.in_(ids))
        )
        return {r for r in rows if r is not None}


async def test_tick_enqueues_trusted_events_once_and_never_untrusted(sf: SF) -> None:
    clock = FakeClock(base(1))
    trusted_text = await ev(sf, at=base(1), lane="trusted")
    trusted_voice = await ev(sf, at=base(1), lane="trusted", kind="voice_transcript")
    fwd = await ev(sf, at=base(1), lane="untrusted")
    rec = await ev(sf, at=base(1), lane="untrusted", kind="recorder_transcript")
    voice_audio = await ev(sf, at=base(1), lane="trusted", kind="voice", body=None)
    short = await ev(sf, at=base(1), lane="trusted", body="too short")
    sweeper = MemoryExtractSweeper(sf, JobQueue(sf, clock), clock)
    everything = [trusted_text, trusted_voice, fwd, rec, voice_audio, short]

    await sweeper.tick()
    assert await jobs_for(sf, everything) == {trusted_text, trusted_voice}
    await sweeper.tick()  # idempotent
    await sweeper.tick()
    async with sf() as s:
        n = len(list(await s.scalars(select(Job.id).where(Job.source_event_id.in_(everything)))))
    assert n == 2


async def test_tick_ignores_events_older_than_the_lookback(sf: SF) -> None:
    clock = FakeClock(base(2))
    old = await ev(sf, at=base(2), lane="trusted")
    sweeper = MemoryExtractSweeper(sf, JobQueue(sf, clock), clock, lookback=timedelta(days=2))
    clock.advance(days=3)
    await sweeper.tick()
    assert await jobs_for(sf, [old]) == set()


async def test_backlog_after_downtime_is_enqueued_at_most_ten_a_minute(sf: SF) -> None:
    clock = FakeClock(base(3))
    ids = [await ev(sf, at=base(3), lane="trusted") for _ in range(25)]
    sweeper = MemoryExtractSweeper(sf, JobQueue(sf, clock), clock)
    await sweeper.tick()
    first = len(await jobs_for(sf, ids))
    assert 0 < first <= 10
    await sweeper.tick()  # same instant: nothing more
    assert len(await jobs_for(sf, ids)) == first
    clock.advance(seconds=60)
    await sweeper.tick()
    assert len(await jobs_for(sf, ids)) <= first + 10
    clock.advance(minutes=10)
    await sweeper.tick()
    clock.advance(minutes=10)
    await sweeper.tick()
    assert len(await jobs_for(sf, ids)) == 25  # drained, never faster than the cap


async def _kill(sf: SF, eid: uuid.UUID, error: str, when: datetime) -> None:
    async with sf() as s, s.begin():
        await s.execute(
            update(Job)
            .where(Job.source_event_id == eid)
            .values(status="dead", last_error=error, updated_at=when, locked_by=None)
        )


async def test_a_job_that_died_of_a_transient_cause_is_enqueued_again(sf: SF) -> None:
    clock = FakeClock(base(4))
    sweeper = MemoryExtractSweeper(sf, JobQueue(sf, clock), clock)
    eid = await ev(sf, at=base(4), lane="trusted")
    await sweeper.tick()
    await _kill(sf, eid, "LLMProviderError", clock.now())
    clock.advance(minutes=10)
    await sweeper.tick()
    async with sf() as s:
        assert (await s.scalar(select(Job.status).where(Job.source_event_id == eid))) == "dead"
    clock.advance(hours=1)  # cool-down over
    await sweeper.tick()
    async with sf() as s:
        row = (await s.execute(select(Job).where(Job.source_event_id == eid))).scalar_one()
    assert (row.status, row.attempts) == ("pending", 0)


async def test_a_job_that_died_permanently_is_not_retried_by_the_sweeper(sf: SF) -> None:
    clock = FakeClock(base(5))
    sweeper = MemoryExtractSweeper(sf, JobQueue(sf, clock), clock)
    eid = await ev(sf, at=base(5), lane="trusted")
    await sweeper.tick()
    await _kill(sf, eid, "scrub_blocked", clock.now())
    clock.advance(hours=5)
    await sweeper.tick()
    async with sf() as s:
        assert (await s.scalar(select(Job.status).where(Job.source_event_id == eid))) == "dead"
