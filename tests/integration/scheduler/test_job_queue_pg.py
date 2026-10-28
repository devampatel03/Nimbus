# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Postgres job queue and runner (P3.3, ADR-0008): SKIP LOCKED claims, leases,
retries with backoff, dead letter, idempotency, crash recovery, graceful cancel.
Real Postgres, fake clock."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.schemas.ops import Job
from src.scheduler.jobs import JobQueue
from src.scheduler.runner import JobContext, JobRunner, PermanentJobError
from tests.fakes.clock import FakeClock

SF = async_sessionmaker[AsyncSession]


async def row(sf: SF, key: str) -> Job:
    async with sf() as s:
        got = (await s.execute(select(Job).where(Job.dedupe_key == key))).scalar_one()
        return got


def make(sf: SF, clock: FakeClock, **kw: object) -> JobQueue:
    return JobQueue(sf, clock, **kw)  # type: ignore[arg-type]


async def test_enqueue_is_idempotent_on_dedupe_key(sf: SF, clock: FakeClock) -> None:
    q = make(sf, clock)
    first, created = await q.enqueue("t", dedupe_key="k1", payload={"a": 1})
    again, created2 = await q.enqueue("t", dedupe_key="k1", payload={"a": 2})
    assert created and not created2
    assert first == again
    assert (await row(sf, "k1")).payload == {"a": 1}  # the original survives


async def test_future_job_is_not_claimed_until_due(sf: SF, clock: FakeClock) -> None:
    q = make(sf, clock)
    await q.enqueue("t", dedupe_key="later", run_at=clock.now() + timedelta(minutes=5))
    assert (await q.claim(kinds=frozenset({"t"}), limit=10)).jobs == []
    clock.advance(minutes=5)
    got = await q.claim(kinds=frozenset({"t"}), limit=10)
    assert [j.dedupe_key for j in got.jobs] == ["later"]
    assert got.jobs[0].attempts == 1


async def test_claim_only_takes_requested_kinds_and_orders_by_run_at(
    sf: SF, clock: FakeClock
) -> None:
    q = make(sf, clock)
    now = clock.now()
    await q.enqueue("b", dedupe_key="b1", run_at=now - timedelta(minutes=1))
    await q.enqueue("a", dedupe_key="a2", run_at=now - timedelta(minutes=2))
    await q.enqueue("a", dedupe_key="a1", run_at=now - timedelta(minutes=3))
    got = await q.claim(kinds=frozenset({"a"}), limit=10)
    assert [j.dedupe_key for j in got.jobs] == ["a1", "a2"]
    assert (await row(sf, "b1")).status == "pending"


async def test_two_runners_racing_run_each_job_exactly_once(sf: SF, clock: FakeClock) -> None:
    ran: list[str] = []

    async def handler(ctx: JobContext) -> None:
        await asyncio.sleep(0.01)
        ran.append(ctx.dedupe_key)

    n = 40
    q1 = JobQueue(sf, clock, worker_id="w1")
    q2 = JobQueue(sf, clock, worker_id="w2")
    for i in range(n):
        await q1.enqueue("t", dedupe_key=f"job-{i}", run_at=clock.now())
    r1 = JobRunner(q1, {"t": handler}, clock=clock, batch=7)
    r2 = JobRunner(q2, {"t": handler}, clock=clock, batch=7)

    async def drain(r: JobRunner) -> None:
        while await r.run_due():
            pass

    await asyncio.gather(drain(r1), drain(r2))
    assert sorted(ran) == sorted(f"job-{i}" for i in range(n))  # none lost, none twice
    async with sf() as s:
        statuses = (await s.execute(select(Job.status, Job.attempts))).all()
    assert {(st, a) for st, a in statuses} == {("done", 1)}


async def test_crash_mid_job_is_recovered_after_the_lease(sf: SF, clock: FakeClock) -> None:
    q = JobQueue(sf, clock, worker_id="crashed", lease_s=60)
    await q.enqueue("t", dedupe_key="c1", run_at=clock.now())
    claimed = await q.claim(kinds=frozenset({"t"}), limit=5)
    assert len(claimed.jobs) == 1  # ... and the process dies here: never completed

    ran: list[int] = []

    async def handler(ctx: JobContext) -> None:
        ran.append(ctx.attempt)

    q2 = JobQueue(sf, clock, worker_id="restarted", lease_s=60)
    runner = JobRunner(q2, {"t": handler}, clock=clock)
    clock.advance(seconds=30)
    assert await runner.run_due() == 0  # lease still held: not stolen early
    clock.advance(seconds=31)
    assert await runner.run_due() == 1
    assert ran == [2]
    j = await row(sf, "c1")
    assert (j.status, j.attempts, j.locked_by) == ("done", 2, None)


async def test_stale_finish_from_a_lost_lease_is_ignored(sf: SF, clock: FakeClock) -> None:
    slow = JobQueue(sf, clock, worker_id="slow", lease_s=60)
    await slow.enqueue("t", dedupe_key="s1", run_at=clock.now())
    [old] = (await slow.claim(kinds=frozenset({"t"}), limit=1)).jobs
    clock.advance(seconds=61)
    fast = JobQueue(sf, clock, worker_id="fast", lease_s=60)
    [new] = (await fast.claim(kinds=frozenset({"t"}), limit=1)).jobs
    assert new.attempts == 2
    assert await slow.complete(old) is False  # the first worker woke up too late
    assert (await row(sf, "s1")).status == "running"
    assert await fast.complete(new) is True


async def test_retries_back_off_then_dead_letter(sf: SF, clock: FakeClock) -> None:
    calls = 0
    dead: list[str] = []

    async def poison(ctx: JobContext) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(
            "secret-looking sk-abcdefghijklmnopqrstuvwxyz0123456789"
        )  # pragma: allowlist secret

    async def on_dead(ctx: JobContext) -> None:
        dead.append(ctx.dedupe_key)

    q = JobQueue(sf, clock, backoff_base_s=30, backoff_max_s=3600)
    await q.enqueue("t", dedupe_key="p1", run_at=clock.now(), max_attempts=3)
    runner = JobRunner(q, {"t": poison}, clock=clock, on_dead={"t": on_dead})

    assert await runner.run_due() == 1
    j = await row(sf, "p1")
    assert (j.status, j.attempts) == ("pending", 1)
    assert j.run_at == clock.now() + timedelta(seconds=30)
    assert j.last_error == "RuntimeError"  # type name only: never the message (INV-7)

    assert await runner.run_due() == 0  # backing off
    clock.advance(seconds=30)
    assert await runner.run_due() == 1
    j = await row(sf, "p1")
    assert j.run_at == clock.now() + timedelta(seconds=60)  # exponential

    clock.advance(seconds=60)
    assert await runner.run_due() == 1
    j = await row(sf, "p1")
    assert (j.status, j.attempts) == ("dead", 3)
    assert calls == 3
    assert dead == ["p1"]
    clock.advance(days=1)
    assert await runner.run_due() == 0  # a dead job never runs again


async def test_permanent_error_dead_letters_immediately(sf: SF, clock: FakeClock) -> None:
    async def bad(ctx: JobContext) -> None:
        raise PermanentJobError("event_missing")

    q = JobQueue(sf, clock)
    await q.enqueue("t", dedupe_key="x1", run_at=clock.now())
    await JobRunner(q, {"t": bad}, clock=clock).run_due()
    j = await row(sf, "x1")
    assert (j.status, j.attempts, j.last_error) == ("dead", 1, "event_missing")


async def test_unknown_kind_is_dead_lettered_not_looped(sf: SF, clock: FakeClock) -> None:
    q = JobQueue(sf, clock)
    await q.enqueue("mystery", dedupe_key="m1", run_at=clock.now())
    await JobRunner(q, {}, clock=clock, kinds=frozenset({"mystery"})).run_due()
    assert (await row(sf, "m1")).status == "dead"


async def test_lease_expiry_at_max_attempts_goes_dead_not_forever(sf: SF, clock: FakeClock) -> None:
    dead: list[str] = []

    async def on_dead(ctx: JobContext) -> None:
        dead.append(ctx.dedupe_key)

    q = JobQueue(sf, clock, lease_s=10)
    await q.enqueue("t", dedupe_key="l1", run_at=clock.now(), max_attempts=1)
    await q.claim(kinds=frozenset({"t"}), limit=1)  # crash-looping job: dies in the handler
    clock.advance(seconds=11)

    async def handler(ctx: JobContext) -> None:
        raise AssertionError("must not run again")

    runner = JobRunner(q, {"t": handler}, clock=clock, on_dead={"t": on_dead})
    assert await runner.run_due() == 0
    assert (await row(sf, "l1")).status == "dead"
    assert dead == ["l1"]


async def test_cancel_pending_and_running(sf: SF, clock: FakeClock) -> None:
    q = JobQueue(sf, clock)
    await q.enqueue("t", dedupe_key="n1", run_at=clock.now())
    assert await q.cancel("n1") is True
    assert (await q.claim(kinds=frozenset({"t"}), limit=5)).jobs == []
    assert await q.cancel("n1") is False  # already cancelled
    assert await q.cancel("missing") is False

    await q.enqueue("t", dedupe_key="n2", run_at=clock.now())
    [job] = (await q.claim(kinds=frozenset({"t"}), limit=1)).jobs
    assert await q.cancel("n2") is True  # cancelled while running
    assert await q.complete(job) is False  # the late finish does not resurrect it
    assert (await row(sf, "n2")).status == "cancelled"


async def test_shutdown_cancellation_releases_the_job_without_burning_an_attempt(
    sf: SF, clock: FakeClock
) -> None:
    started = asyncio.Event()

    async def hang(ctx: JobContext) -> None:
        started.set()
        await asyncio.sleep(3600)

    q = JobQueue(sf, clock)
    await q.enqueue("t", dedupe_key="g1", run_at=clock.now())
    task = asyncio.create_task(JobRunner(q, {"t": hang}, clock=clock).run_due())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    j = await row(sf, "g1")
    assert (j.status, j.attempts, j.locked_by) == ("pending", 0, None)  # picked up on restart

    done: list[str] = []

    async def ok(ctx: JobContext) -> None:
        done.append(ctx.dedupe_key)

    assert await JobRunner(q, {"t": ok}, clock=clock).run_due() == 1
    assert done == ["g1"]


async def test_handler_timeout_counts_as_a_failure(sf: SF, clock: FakeClock) -> None:
    async def slow(ctx: JobContext) -> None:
        await asyncio.sleep(5)

    q = JobQueue(sf, clock, lease_s=60)
    await q.enqueue("t", dedupe_key="to1", run_at=clock.now())
    await JobRunner(q, {"t": slow}, clock=clock, handler_timeout_s=0.05).run_due()
    j = await row(sf, "to1")
    assert (j.status, j.last_error) == ("pending", "TimeoutError")


def test_handler_timeout_must_be_inside_the_lease() -> None:
    from tests.fakes.clock import FakeClock as FC

    with pytest.raises(ValueError):
        JobRunner(JobQueue(None, FC(), lease_s=60), {}, clock=FC(), handler_timeout_s=60)  # type: ignore[arg-type]


async def test_enqueue_inside_a_caller_transaction_rolls_back_with_it(
    sf: SF, clock: FakeClock
) -> None:
    q = make(sf, clock)
    with pytest.raises(RuntimeError):
        async with sf() as s, s.begin():
            await q.enqueue_in(s, "t", dedupe_key="atomic", run_at=clock.now())
            raise RuntimeError("boom")
    async with sf() as s:
        n = await s.scalar(text("SELECT count(*) FROM job WHERE dedupe_key = 'atomic'"))
    assert n == 0


async def test_rearm_resets_a_finished_job_but_not_a_running_one(sf: SF, clock: FakeClock) -> None:
    q = make(sf, clock)
    await q.enqueue("t", dedupe_key="r1", run_at=clock.now())
    [job] = (await q.claim(kinds=frozenset({"t"}), limit=1)).jobs
    created = await q.enqueue("t", dedupe_key="r1", run_at=clock.now(), rearm=True)
    assert created[1] is False
    assert (await row(sf, "r1")).status == "running"  # in flight: untouched
    await q.complete(job)
    later = clock.now() + timedelta(hours=1)
    await q.enqueue("t", dedupe_key="r1", run_at=later, rearm=True)
    j = await row(sf, "r1")
    assert (j.status, j.attempts, j.run_at) == ("pending", 0, later)
