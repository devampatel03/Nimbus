# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Persisted reminders (P3.3, R1, R11): stored and scheduled in ONE transaction,
delivered through the gateway within 60 s, catch-up after downtime fires each
due reminder once, edits and deletes cancel the old delivery. Fake clock."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.schemas.ops import Job
from src.memory.schemas.scheduling import Reminder
from src.orchestrator.dispatch import ToolError
from src.scheduler.jobs import JobQueue
from src.scheduler.reminders import KIND, ReminderDelivery, ReminderStore
from src.scheduler.runner import JobRunner
from src.tools.integrations.reminders import RemindersTool
from tests.fakes.clock import FakeClock

SF = async_sessionmaker[AsyncSession]


class RecordingGateway:
    """Only the part of the Gateway the delivery path may use."""

    def __init__(self, fail: int = 0) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, text: str, *, reply_to: Any = None) -> list[Any]:
        if self.fail:
            self.fail -= 1
            raise OSError("telegram down")
        self.sent.append(text)
        return []


def build(sf: SF, clock: FakeClock, gw: RecordingGateway) -> tuple[ReminderStore, JobRunner]:
    q = JobQueue(sf, clock)
    store = ReminderStore(sf, q, clock)
    delivery = ReminderDelivery(sf, gw, clock)  # type: ignore[arg-type]
    runner = JobRunner(q, {KIND: delivery.handle}, clock=clock, on_dead={KIND: delivery.on_dead})
    return store, runner


async def n_reminders(sf: SF) -> int:
    async with sf() as s:
        return int(await s.scalar(select(func.count()).select_from(Reminder)) or 0)


async def n_jobs(sf: SF) -> int:
    async with sf() as s:
        return int(await s.scalar(select(func.count()).select_from(Job)) or 0)


async def test_create_stores_row_and_job_atomically(sf: SF, clock: FakeClock) -> None:
    store, _ = build(sf, clock, RecordingGateway())
    r = await store.create("call mom", clock.now() + timedelta(hours=1))
    assert await n_reminders(sf) == 1
    async with sf() as s:
        job = (await s.execute(select(Job))).scalar_one()
    assert job.kind == KIND and job.dedupe_key.startswith(f"reminder:{r.id}:")
    assert job.run_at == r.remind_at
    assert job.payload["reminder_id"] == str(r.id)


async def test_a_failed_schedule_leaves_no_orphan_reminder(
    sf: SF, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P2.5b review bug: reminder stored, then scheduling failed."""
    store, _ = build(sf, clock, RecordingGateway())

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(JobQueue, "enqueue_in", boom)
    with pytest.raises(RuntimeError):
        await store.create("x", clock.now() + timedelta(hours=1))
    assert await n_reminders(sf) == 0
    assert await n_jobs(sf) == 0


async def test_delivered_within_a_minute_of_due_time(sf: SF, clock: FakeClock) -> None:
    gw = RecordingGateway()
    store, runner = build(sf, clock, gw)
    due = clock.now() + timedelta(minutes=10)
    r = await store.create("stand-up", due)
    clock.advance(minutes=9, seconds=59)
    assert await runner.run_due() == 0 and gw.sent == []
    clock.set(due + timedelta(seconds=5))  # one worker poll later
    assert await runner.run_due() == 1
    assert gw.sent == ["Reminder: stand-up"]  # on time: no "late" label
    got = await store.get(r.id)
    assert got is not None and got.status == "fired" and got.fired_at == clock.now()
    assert await runner.run_due() == 0  # nothing more
    assert len(gw.sent) == 1


async def test_catch_up_after_downtime_fires_each_due_reminder_once(
    sf: SF, clock: FakeClock
) -> None:
    gw = RecordingGateway()
    store, runner = build(sf, clock, gw)
    t0 = clock.now()
    for i, mins in enumerate((5, 15, 30, 600)):
        await store.create(f"r{i}", t0 + timedelta(minutes=mins))
    clock.set(t0 + timedelta(hours=2))  # the app was down for 2 hours
    assert await runner.run_due() == 3
    assert sorted(gw.sent) == sorted(
        [
            "Reminder: r0 (late: was due Tue 27 Oct 2026, 10:05 AM IST)",
            "Reminder: r1 (late: was due Tue 27 Oct 2026, 10:15 AM IST)",
            "Reminder: r2 (late: was due Tue 27 Oct 2026, 10:30 AM IST)",
        ]
    )
    # "restart": a fresh runner/queue on the same database, repeated polls
    store2, runner2 = build(sf, clock, gw)
    for _ in range(3):
        assert await runner2.run_due() == 0
    assert len(gw.sent) == 3  # none lost (r3 is still in the future), none duplicated
    clock.set(t0 + timedelta(hours=11))
    assert await runner2.run_due() == 1
    assert len(gw.sent) == 4 and gw.sent[-1].startswith("Reminder: r3")
    assert store2 is not None


async def test_telegram_outage_retries_then_delivers_once(sf: SF, clock: FakeClock) -> None:
    gw = RecordingGateway(fail=2)
    store, runner = build(sf, clock, gw)
    await store.create("x", clock.now() + timedelta(minutes=1))
    clock.advance(minutes=1)
    for _ in range(4):
        await runner.run_due()
        clock.advance(minutes=2)
    assert len(gw.sent) == 1  # delivered once, labelled late because of the outage
    assert gw.sent[0].startswith("Reminder: x (late: was due")


async def test_dead_lettered_reminder_tells_the_user(sf: SF, clock: FakeClock) -> None:
    gw = RecordingGateway(fail=5)  # exactly max_attempts failures, then Telegram recovers
    store, runner = build(sf, clock, gw)
    await store.create("pay rent", clock.now() + timedelta(minutes=1))
    clock.advance(minutes=1)
    for _ in range(8):
        await runner.run_due()
        clock.advance(hours=1)
    async with sf() as s:
        assert (await s.execute(select(Job.status))).scalar_one() == "dead"
    assert gw.sent == ["I could not deliver your reminder: pay rent"]
    assert await runner.run_due() == 0  # a dead job never runs again
    assert len(gw.sent) == 1


async def test_update_moves_delivery_and_old_time_never_fires(sf: SF, clock: FakeClock) -> None:
    gw = RecordingGateway()
    store, runner = build(sf, clock, gw)
    r = await store.create("a", clock.now() + timedelta(hours=1))
    await store.update(r.id, "b", clock.now() + timedelta(hours=3))
    clock.advance(hours=1, minutes=1)
    assert await runner.run_due() == 0 and gw.sent == []
    clock.advance(hours=2)
    assert await runner.run_due() == 1
    assert gw.sent[0].startswith("Reminder: b")
    assert len(gw.sent) == 1


async def test_update_of_a_fired_reminder_rearms_it(sf: SF, clock: FakeClock) -> None:
    gw = RecordingGateway()
    store, runner = build(sf, clock, gw)
    r = await store.create("a", clock.now() + timedelta(minutes=1))
    clock.advance(minutes=2)
    await runner.run_due()
    await store.update(r.id, "a", clock.now() + timedelta(minutes=5))
    got = await store.get(r.id)
    assert got is not None and got.status == "pending" and got.fired_at is None
    clock.advance(minutes=6)
    await runner.run_due()
    assert len(gw.sent) == 2


async def test_delete_cancels_delivery(sf: SF, clock: FakeClock) -> None:
    gw = RecordingGateway()
    store, runner = build(sf, clock, gw)
    r = await store.create("a", clock.now() + timedelta(minutes=1))
    assert await store.delete(r.id) is True
    assert await store.delete(r.id) is False
    clock.advance(hours=1)
    assert await runner.run_due() == 0 and gw.sent == []


async def test_stale_job_for_a_moved_reminder_is_skipped_by_the_handler(
    sf: SF, clock: FakeClock
) -> None:
    """Defence in depth: even if an old delivery job survives, the handler
    compares the job's time with the row and does not send."""
    gw = RecordingGateway()
    store, runner = build(sf, clock, gw)
    r = await store.create("a", clock.now() + timedelta(minutes=1))
    async with sf() as s, s.begin():
        await s.execute(
            text("UPDATE reminder SET remind_at = remind_at + interval '1 day' WHERE id = :i"),
            {"i": r.id},
        )
    clock.advance(minutes=2)
    assert await runner.run_due() == 1  # job ran (and completed) ...
    assert gw.sent == []  # ... but sent nothing


# -- the tool group (no policy here; DenyAllWritesPolicy is tested elsewhere) ----


def tool_group(
    sf: SF, clock: FakeClock, gw: RecordingGateway
) -> tuple[RemindersTool, dict[str, Any]]:
    store, _ = build(sf, clock, gw)
    group = RemindersTool()
    group.bind(store, clock)
    return group, {t.spec.name: t for t in group.tools()}


async def test_tool_create_confirms_the_absolute_ist_time(sf: SF, clock: FakeClock) -> None:
    _, tools = tool_group(sf, clock, RecordingGateway())
    out = await tools["reminders_create"].run({"title": "call mom", "remind_at": "tomorrow 9am"})
    body = json.loads(out.text)
    assert body["remind_at"] == "Wed 28 Oct 2026, 9:00 AM IST"
    assert body["confirmation"].startswith("Wed 28 Oct 2026, 9:00 AM IST (in 23 hours)")
    assert body["remind_at_utc"] == "2026-10-28T03:30:00+00:00"


async def test_tool_crud_round_trip(sf: SF, clock: FakeClock) -> None:
    _, tools = tool_group(sf, clock, RecordingGateway())
    created = await tools["reminders_create"].run(
        {"title": "call mom", "remind_at": "2026-11-01T09:00"}
    )
    rid = json.loads(created.text)["reminder_id"]
    got = json.loads((await tools["reminders_get"].run({"reminder_id": rid})).text)
    assert (got["title"], got["fired"]) == ("call mom", False)
    await tools["reminders_update"].run(
        {"reminder_id": rid, "title": "call dad", "remind_at": "in 3 days"}
    )
    listed = json.loads((await tools["reminders_list"].run({})).text)
    assert listed["count"] == 1 and listed["reminders"][0]["title"] == "call dad"
    await tools["reminders_delete"].run({"reminder_id": rid})
    with pytest.raises(ToolError, match="reminder not found"):
        await tools["reminders_get"].run({"reminder_id": rid})
    with pytest.raises(ToolError, match="reminder not found"):
        await tools["reminders_get"].run({"reminder_id": "not-a-uuid"})


@pytest.mark.parametrize(
    "when", ["not a date", "yesterday 9am", "at 5", "31 Feb 2027", "2026-10-27T09:00"]
)
async def test_tool_rejects_bad_times_without_storing(sf: SF, clock: FakeClock, when: str) -> None:
    _, tools = tool_group(sf, clock, RecordingGateway())
    with pytest.raises(ToolError):
        await tools["reminders_create"].run({"title": "x", "remind_at": when})
    assert await n_reminders(sf) == 0 and await n_jobs(sf) == 0


@pytest.mark.parametrize("title", ["", "   ", "x" * 501])
async def test_tool_rejects_bad_titles(sf: SF, clock: FakeClock, title: str) -> None:
    _, tools = tool_group(sf, clock, RecordingGateway())
    with pytest.raises(ToolError):
        await tools["reminders_create"].run({"title": title, "remind_at": "tomorrow 9am"})
    assert await n_reminders(sf) == 0


async def test_unbound_group_fails_closed() -> None:
    tools = {t.spec.name: t for t in RemindersTool().tools()}
    with pytest.raises(ToolError, match="unavailable"):
        await tools["reminders_list"].run({})
