# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The scheduler tick that backstops the post-turn enqueue: every trusted user
text/voice-transcript event gets exactly one `memory_extract` job, including the
ones whose turn crashed before the enqueue. Untrusted events never do."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.ops import Job
from src.scheduler.jobs import JobQueue
from src.scheduler.memory_extract import KIND, MemoryExtractSweeper
from tests.fakes.clock import FakeClock

SF = async_sessionmaker[AsyncSession]


async def ev(
    sf: SF, *, lane: str, kind: str = "text", body: str | None = "remember this"
) -> uuid.UUID:
    async with sf() as s, s.begin():
        e = EventRow(
            source_key=f"sw:{uuid.uuid4()}",
            kind=kind,
            source="telegram",
            lane=lane,
            body=body,
            meta={},
            occurred_at=datetime.now(UTC),
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
    clock = FakeClock(datetime.now(UTC))
    trusted_text = await ev(sf, lane="trusted")
    trusted_voice = await ev(sf, lane="trusted", kind="voice_transcript")
    fwd = await ev(sf, lane="untrusted")
    rec = await ev(sf, lane="untrusted", kind="recorder_transcript")
    voice_audio = await ev(sf, lane="trusted", kind="voice", body=None)
    sweeper = MemoryExtractSweeper(sf, JobQueue(sf, clock), clock)
    everything = [trusted_text, trusted_voice, fwd, rec, voice_audio]

    await sweeper.tick()
    assert await jobs_for(sf, everything) == {trusted_text, trusted_voice}
    await sweeper.tick()  # idempotent
    await sweeper.tick()
    async with sf() as s:
        n = len(list(await s.scalars(select(Job.id).where(Job.source_event_id.in_(everything)))))
    assert n == 2


async def test_tick_ignores_events_older_than_the_lookback(sf: SF) -> None:
    clock = FakeClock(datetime.now(UTC))
    old = await ev(sf, lane="trusted")
    sweeper = MemoryExtractSweeper(sf, JobQueue(sf, clock), clock, lookback=timedelta(days=2))
    clock.advance(days=3)
    await sweeper.tick()
    assert await jobs_for(sf, [old]) == set()
