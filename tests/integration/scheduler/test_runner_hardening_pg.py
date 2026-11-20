# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P3.3 review round 1: claim exclusivity under a held transaction, reminder
isolation from slow jobs, progress beats, deferral, boot recovery, purge, and
dead-letter hooks that cannot stall a batch."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.schemas.ops import Job
from src.scheduler.jobs import JobQueue
from src.scheduler.reminders import KIND as REMINDER
from src.scheduler.reminders import ReminderDelivery, ReminderStore
from src.scheduler.runner import JobContext, JobDeferred, JobRunner, PermanentJobError
from tests.fakes.clock import FakeClock

SF = async_sessionmaker[AsyncSession]


async def row(sf: SF, key: str) -> Job:
    async with sf() as s:
        return (await s.execute(select(Job).where(Job.dedupe_key == key))).scalar_one()


async def test_concurrent_claim_never_double_claims_while_a_claim_tx_is_open(
    sf: SF, clock: FakeClock
) -> None:
    """Runner A's claim transaction is open (rows updated, not committed) when
    runner B claims: B must skip A's locked rows (FOR UPDATE SKIP LOCKED)."""
    qa = JobQueue(sf, clock, worker_id="A")
    qb = JobQueue(sf, clock, worker_id="B")
    for i in range(3):
        await qa.enqueue("t", dedupe_key=f"r{i}", run_at=clock.now())
    async with sf() as s:
        await s.begin()
        await s.execute(
            text(
                "UPDATE job SET status='running', attempts=attempts+1, locked_by='A', "
                "locked_at=:n WHERE id IN (SELECT id FROM job WHERE status='pending' "
                "ORDER BY run_at LIMIT 3 FOR UPDATE SKIP LOCKED)"
            ),
            {"n": clock.now()},
        )
        b_task = asyncio.create_task(qb.claim(kinds=frozenset({"t"}), limit=3))
        # SKIP LOCKED: B must not WAIT for A's open transaction either (plain
        # FOR UPDATE would block here until A commits). Poll up to 5 s so a
        # loaded machine does not flake; a blocked B never finishes in time.
        for _ in range(50):
            if b_task.done():
                break
            await asyncio.sleep(0.1)
        b_finished_while_a_open = b_task.done()
        await s.commit()
    got = await asyncio.wait_for(b_task, 5)
    assert b_finished_while_a_open, "B blocked on A's locked rows (not SKIP LOCKED)"
    async with sf() as s:
        rows = (await s.execute(select(Job.dedupe_key, Job.locked_by, Job.attempts))).all()
    assert got.jobs == [], f"B double-claimed {[j.dedupe_key for j in got.jobs]}; rows={rows}"


async def test_a_slow_job_in_another_runner_does_not_delay_a_due_reminder(
    sf: SF, clock: FakeClock
) -> None:
    """The R11 HIGH: reminders run in their OWN runner, so a slow background job
    (3 LLM calls in the old design) cannot hold them back."""
    delivered: list[float] = []
    t0 = time.monotonic()

    async def slow(ctx: JobContext) -> None:
        await asyncio.sleep(2.0)

    async def reminder(ctx: JobContext) -> None:
        delivered.append(time.monotonic() - t0)

    qb = JobQueue(sf, clock)
    qr = JobQueue(sf, clock, lease_s=60)
    background = JobRunner(qb, {"memory_extract": slow}, clock=clock)
    reminders = JobRunner(qr, {REMINDER: reminder}, clock=clock)
    await qb.enqueue("memory_extract", dedupe_key="s", run_at=clock.now())
    await qb.enqueue(REMINDER, dedupe_key="r", run_at=clock.now() + timedelta(seconds=1))

    async def bg() -> None:
        await background.run_due()

    async def rem() -> None:
        while not delivered:
            await reminders.run_due()
            await asyncio.sleep(0.05)

    async def advance() -> None:
        await asyncio.sleep(0.2)
        clock.advance(seconds=1)  # the reminder falls due while the slow job runs

    await asyncio.gather(bg(), rem(), advance())
    assert delivered[0] < 0.6, f"reminder due at 0.2 s delivered at {delivered[0]:.2f} s"
    assert (await row(sf, "s")).status == "done"  # the slow job still completed


async def test_runner_isolation_by_kind(sf: SF, clock: FakeClock) -> None:
    ran: list[str] = []

    async def h(ctx: JobContext) -> None:
        ran.append(ctx.kind)

    q = JobQueue(sf, clock)
    await q.enqueue("a", dedupe_key="a1", run_at=clock.now())
    await q.enqueue("b", dedupe_key="b1", run_at=clock.now())
    only_a = JobRunner(q, {"a": h, "b": h}, clock=clock, kinds=frozenset({"a"}))
    assert await only_a.run_due() == 1 and ran == ["a"]
    assert (await row(sf, "b1")).status == "pending"


async def test_progress_is_reported_per_claim_and_per_finished_job(
    sf: SF, clock: FakeClock
) -> None:
    beats: list[str] = []

    async def h(ctx: JobContext) -> None:
        beats.append("running")

    q = JobQueue(sf, clock)
    runner = JobRunner(q, {"t": h}, clock=clock, on_progress=lambda: beats.append("beat"))
    for i in range(2):
        await q.enqueue("t", dedupe_key=f"p{i}", run_at=clock.now())
    await runner.run_due()
    # one beat after the claim (before any handler runs), one per finished job
    assert beats[0] == "beat"
    assert beats.count("beat") == 3
    assert beats[-1] == "beat"


async def test_handler_timeout_bounds_the_gap_between_beats(sf: SF, clock: FakeClock) -> None:
    """A stuck handler ends at its timeout and the runner beats again, so the
    worker heartbeat cannot go stale for longer than the timeout."""
    stamps: list[float] = []

    async def stuck(ctx: JobContext) -> None:
        await asyncio.sleep(30)

    q = JobQueue(sf, clock, lease_s=60)
    runner = JobRunner(
        q,
        {"t": stuck},
        clock=clock,
        handler_timeout_s=0.2,
        on_progress=lambda: stamps.append(time.monotonic()),
    )
    await q.enqueue("t", dedupe_key="stuck", run_at=clock.now())
    await runner.run_due()
    assert len(stamps) == 2 and stamps[1] - stamps[0] < 1.0


async def test_deferred_job_keeps_its_attempt_and_runs_later(sf: SF, clock: FakeClock) -> None:
    calls: list[int] = []
    later = clock.now() + timedelta(minutes=30)

    async def h(ctx: JobContext) -> None:
        calls.append(ctx.attempt)
        if len(calls) == 1:
            raise JobDeferred(later)

    q = JobQueue(sf, clock)
    await q.enqueue("t", dedupe_key="d1", run_at=clock.now(), max_attempts=2)
    runner = JobRunner(q, {"t": h}, clock=clock)
    assert await runner.run_due() == 1
    j = await row(sf, "d1")
    assert (j.status, j.attempts, j.run_at, j.locked_by) == ("pending", 0, later, None)
    for _ in range(5):  # deferring never burns attempts, however often
        clock.set(later)
        later = later + timedelta(minutes=30)
        calls.clear()
        calls.append(0)
        assert await runner.run_due() == 1
        j = await row(sf, "d1")
        if j.status == "done":
            break
    assert j.status == "done" and j.attempts == 1


async def test_boot_recovery_releases_running_rows_and_frees_one_attempt(
    sf: SF, clock: FakeClock
) -> None:
    old = JobQueue(sf, clock, worker_id="previous-process")
    await old.enqueue("t", dedupe_key="o1", run_at=clock.now())
    await old.claim(kinds=frozenset({"t"}), limit=1)  # ... the process dies
    assert (await row(sf, "o1")).attempts == 1

    fresh = JobQueue(sf, clock, worker_id="new-process")
    assert await fresh.recover_orphans() == 1
    j = await row(sf, "o1")
    assert (j.status, j.attempts, j.locked_by) == ("pending", 0, None)

    # a second consecutive interruption (crash loop) DOES burn the attempt
    await fresh.claim(kinds=frozenset({"t"}), limit=1)
    third = JobQueue(sf, clock, worker_id="third")
    await third.recover_orphans()
    assert (await row(sf, "o1")).attempts == 1
    await third.claim(kinds=frozenset({"t"}), limit=1)
    await JobQueue(sf, clock, worker_id="fourth").recover_orphans()
    assert (await row(sf, "o1")).attempts == 2


async def test_a_job_that_kills_the_process_dies_after_max_attempts(
    sf: SF, clock: FakeClock
) -> None:
    """HIGH-1: crash-loop boots must not run a poison job forever."""
    await JobQueue(sf, clock).enqueue("t", dedupe_key="poison", run_at=clock.now(), max_attempts=3)
    runs = 0
    for boot in range(10):
        q = JobQueue(sf, clock, worker_id=f"boot{boot}")
        await q.recover_orphans()
        got = await q.claim(kinds=frozenset({"t"}), limit=1)
        runs += len(got.jobs)  # the handler "kills the process" here: never finishes
    j = await row(sf, "poison")
    assert j.status == "dead" and j.last_error == "CrashLoop"
    assert runs <= 3 + 1, runs


async def test_pending_row_at_max_attempts_is_dead_lettered_not_claimed(
    sf: SF, clock: FakeClock
) -> None:
    q = JobQueue(sf, clock)
    await q.enqueue("t", dedupe_key="spent", run_at=clock.now(), max_attempts=2)
    async with sf() as s, s.begin():
        await s.execute(text("UPDATE job SET attempts = 2 WHERE dedupe_key='spent'"))
    got = await q.claim(kinds=frozenset({"t"}), limit=5)
    assert got.jobs == [] and (await row(sf, "spent")).status == "dead"


class Gw:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str, *, reply_to: Any = None) -> list[Any]:
        self.sent.append(text)
        return []


async def test_in_flight_reminder_is_delivered_within_90s_of_restart(
    sf: SF, clock: FakeClock
) -> None:
    """quality/thresholds restart_recovery_seconds_max = 90."""
    q1 = JobQueue(sf, clock, lease_s=60)
    store = ReminderStore(sf, q1, clock)
    await store.create("dentist", clock.now() + timedelta(minutes=1))
    clock.advance(minutes=1)
    await q1.claim(kinds=frozenset({REMINDER}), limit=1)  # crash while delivering
    restart_at = clock.now()

    gw = Gw()
    q2 = JobQueue(sf, clock, lease_s=60)  # new process
    delivery = ReminderDelivery(sf, gw, clock)  # type: ignore[arg-type]
    runner = JobRunner(q2, {REMINDER: delivery.handle}, clock=clock)
    # Without boot recovery the short reminder lease still bounds it to 60 s:
    assert await runner.run_due() == 0
    clock.advance(seconds=61)
    assert await runner.run_due() == 1
    assert clock.now() - restart_at <= timedelta(seconds=90)
    assert len(gw.sent) == 1

    # With boot recovery it is immediate.
    gw2 = Gw()
    clock.advance(minutes=5)
    await store.create("second", clock.now() + timedelta(minutes=1))
    clock.advance(minutes=1)
    await q2.claim(kinds=frozenset({REMINDER}), limit=1)
    q3 = JobQueue(sf, clock, lease_s=60)
    await q3.recover_orphans()
    runner3 = JobRunner(
        q3,
        {REMINDER: ReminderDelivery(sf, gw2, clock).handle},
        clock=clock,  # type: ignore[arg-type]
    )
    assert await runner3.run_due() == 1 and len(gw2.sent) == 1


async def test_purge_removes_only_old_finished_jobs(sf: SF, clock: FakeClock) -> None:
    q = JobQueue(sf, clock)
    for k in ("done", "dead", "cancelled", "pending", "running"):
        await q.enqueue("t", dedupe_key=k, run_at=clock.now())
    async with sf() as s, s.begin():
        await s.execute(text("UPDATE job SET status = dedupe_key"))
    clock.advance(days=31)
    await q.enqueue("t", dedupe_key="recent-done", run_at=clock.now())
    async with sf() as s, s.begin():
        await s.execute(
            text("UPDATE job SET status='done', updated_at=:n WHERE dedupe_key='recent-done'"),
            {"n": clock.now()},
        )
    assert await q.purge_finished(older_than=timedelta(days=30)) == 3
    async with sf() as s:
        left = set(await s.scalars(select(Job.dedupe_key)))
    assert left == {"pending", "running", "recent-done"}


async def test_dead_letter_hook_cannot_stall_the_batch(sf: SF, clock: FakeClock) -> None:
    async def bad(ctx: JobContext) -> None:
        raise PermanentJobError("nope")

    async def ok(ctx: JobContext) -> None:
        return None

    async def hang(ctx: JobContext) -> None:
        await asyncio.sleep(30)

    q = JobQueue(sf, clock)
    await q.enqueue("bad", dedupe_key="b", run_at=clock.now())
    await q.enqueue("ok", dedupe_key="o", run_at=clock.now())
    runner = JobRunner(
        q, {"bad": bad, "ok": ok}, clock=clock, on_dead={"bad": hang}, dead_hook_timeout_s=0.2
    )
    t0 = time.monotonic()
    assert await runner.run_due() == 2
    assert time.monotonic() - t0 < 2.0
    assert (await row(sf, "o")).status == "done"
    assert (await row(sf, "b")).status == "dead"
