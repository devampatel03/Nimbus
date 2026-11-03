# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P5.1 proactive jobs on real Postgres with a fake clock (R11, ADR-0011,
ADR-0008): templates only (no model call), budget <= 6/day across kinds, quiet
hours and snooze defer, one message per job per IST day (also across a restart),
catch-up sends at most one missed brief, nudges once per commitment per day, the
weekly review drains the ReviewQueue (bounded), and a slow proactive job never
blocks a reminder."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.timeparse import IST
from src.memory.schemas.ops import Job
from src.memory.schemas.scheduling import Task
from src.scheduler import proactive as pr
from src.scheduler import proactive_text as tx
from src.scheduler import reminders as rem
from src.scheduler.jobs import JobQueue
from src.scheduler.proactive_state import ProactiveState, Reservation
from src.scheduler.runner import JobRunner
from src.scheduler.settings import ProactiveSettings
from tests._approval_helpers import isolated_schema
from tests._ledger_helpers import LedgerHarness, make_event
from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.asyncio
SF = async_sessionmaker[AsyncSession]


def ist(h: int, m: int = 0, day: int = 2) -> datetime:  # Mon 2 Nov 2026
    return datetime(2026, 11, day, h, m, tzinfo=IST).astimezone(UTC)


class Gw:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail = 0

    async def send_text(self, text: str, *, reply_to: Any = None) -> list[Any]:
        if self.fail:
            self.fail -= 1
            raise OSError("down")
        self.sent.append(text)
        return []


class Cal:
    def __init__(self, titles: list[str] | None = None, delay: float = 0.0) -> None:
        self.titles = titles or []
        self.delay = delay

    async def between(self, start: datetime, end: datetime, limit: int) -> list[tx.CalItem]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return [tx.CalItem(t, start + timedelta(hours=10), False) for t in self.titles]


class Review:
    def __init__(self, pending: int = 0) -> None:
        self.pending = pending
        self.drained: list[int] = []

    async def pending_count(self) -> int:
        return self.pending

    async def drain(self, *, max_cards: int) -> int:
        self.drained.append(max_cards)
        return min(max_cards, self.pending)


@pytest.fixture
async def sf() -> AsyncIterator[SF]:
    async with isolated_schema() as (_, factory):
        yield factory


class Rig:
    def __init__(self, sf: SF, clock: FakeClock, **over: Any) -> None:
        self.sf, self.clock = sf, clock
        self.gw = Gw()
        self.lh = LedgerHarness(sf)
        self.cfg = ProactiveSettings(_env_file=None, **over)  # type: ignore[call-arg]
        self.queue = JobQueue(sf, clock)
        self.review = Review()
        self.cal: Any = None
        self.parts = self.build()

    def build(self, **kw: Any) -> pr.ProactiveParts:
        return pr.build_proactive(
            self.sf,
            queue=self.queue,
            gateway=self.gw,  # type: ignore[arg-type]
            ledger=self.lh.ledger,
            settings=self.cfg,
            clock=self.clock,
            review=self.review,  # type: ignore[arg-type]
            calendar=self.cal,
            **kw,
        )

    async def cycle(self) -> int:
        """One scheduler tick then one runner pass."""
        await self.parts.scheduler.tick()
        return await self.parts.runner.run_due()

    async def confirmed(self, desc: str, due_phrase: str) -> uuid.UUID:
        body = f"I will {desc} by {due_phrase}"
        ev = await make_event(self.sf, body)
        (cand,) = await self.lh.intake.on_extracted(ev, [(desc, 0.9, due_phrase)])
        await self.lh.h.tap(self.lh.h.last_card().approve_data)
        return cand.key


async def add_task(sf: SF, title: str = "buy milk") -> None:
    async with sf() as s, s.begin():
        s.add(Task(title=title, done=False))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(ist(8, 31))  # just after the morning slot


async def test_morning_brief_is_a_template_with_no_model_call(sf: SF, clock: FakeClock) -> None:
    rig = Rig(sf, clock, evening_enabled=False, nudges_enabled=False)
    rig.cal = Cal(["Standup"])
    await rig.confirmed("send the deck", "2 Nov 2026 5pm")
    await rig.confirmed("file taxes", "30 Oct 2026 5pm")
    await add_task(sf)
    ev = await make_event(sf, "I will call Sam by 1 Nov 2026 5pm")
    await rig.lh.intake.on_extracted(ev, [("call Sam", 0.9, "1 Nov 2026 5pm")])  # candidate only

    await rig.cycle()

    (text,) = rig.gw.sent
    assert "Your brief for Mon 02 Nov" in text
    assert "“Standup”" in text
    assert "“send the deck”, due 5:00 PM" in text and "“file taxes”, was due" in text
    assert "“buy milk”" in text and "1 commitment candidate(s)" in text


async def test_calendar_unbound_skips_section_and_empty_brief_is_not_sent(
    sf: SF, clock: FakeClock
) -> None:
    rig = Rig(sf, clock, evening_enabled=False)
    await rig.cycle()
    assert rig.gw.sent == []  # nothing to say
    await add_task(sf)
    rig2 = Rig(sf, FakeClock(ist(8, 31, day=3)), evening_enabled=False)
    await rig2.cycle()
    assert len(rig2.gw.sent) == 1 and "Calendar" not in rig2.gw.sent[0]


async def test_calendar_failure_does_not_kill_the_brief(sf: SF, clock: FakeClock) -> None:
    class Boom:
        async def between(self, *a: Any) -> list[tx.CalItem]:
            raise RuntimeError("secret detail")

    rig = Rig(sf, clock, evening_enabled=False)
    rig.cal = Boom()
    rig.parts = rig.build()
    await add_task(sf)
    await rig.cycle()
    assert "could not be read" in rig.gw.sent[0] and "secret detail" not in rig.gw.sent[0]


async def test_same_job_same_day_sends_once_also_across_restart(sf: SF, clock: FakeClock) -> None:
    rig = Rig(sf, clock, evening_enabled=False)
    await add_task(sf)
    await rig.cycle()
    await rig.cycle()
    rig.parts = rig.build()  # restart: fresh scheduler memory
    await rig.cycle()
    clock.advance(minutes=30)
    await rig.cycle()
    assert len(rig.gw.sent) == 1
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Job)) == 1
    clock.set(ist(8, 31, day=3))  # next IST day: a new key
    await rig.cycle()
    assert len(rig.gw.sent) == 2


async def test_state_reserve_dedupes_a_key(sf: SF, clock: FakeClock) -> None:
    st = ProactiveState(sf, clock)
    assert await st.reserve("k", "proactive_morning", budget=6) is Reservation.OK
    assert await st.reserve("k", "proactive_morning", budget=6) is Reservation.DUPLICATE


async def test_budget_caps_total_per_ist_day_across_kinds(sf: SF, clock: FakeClock) -> None:
    st = ProactiveState(sf, clock)
    for i in range(6):
        assert await st.reserve(f"k{i}", f"kind{i % 3}", budget=6) is Reservation.OK
    assert await st.reserve("k6", "x", budget=6) is Reservation.OVER_BUDGET
    clock.set(datetime(2026, 11, 2, 18, 29, tzinfo=UTC))  # 23:59 IST: still the same IST day
    assert await st.reserve("k7", "x", budget=6) is Reservation.OVER_BUDGET
    clock.set(datetime(2026, 11, 2, 18, 31, tzinfo=UTC))  # 00:01 IST next day: resets
    assert await st.reserve("k7", "x", budget=6) is Reservation.OK
    assert (await st.snapshot()).sent_today == 1


async def test_budget_applies_to_delivery_and_nudges_leave_room_for_briefs(
    sf: SF, clock: FakeClock
) -> None:
    rig = Rig(sf, clock, budget_per_day=3, evening_valid_h=18, weekly_weekday=0, nudge_scan_s=1)
    await add_task(sf)
    for i in range(3):
        await rig.confirmed(f"do thing {i}", "2 Nov 2026 11pm")
    clock.set(ist(20, 40))  # evening + weekly live; morning (valid 6h) expired
    for _ in range(3):
        await rig.cycle()
        clock.advance(seconds=5)
    # budget 3, two reserved for the briefs: evening + weekly + at most one nudge
    nudges = [t for t in rig.gw.sent if t.startswith("Nudge")]
    assert len(nudges) <= 1
    assert len(rig.gw.sent) <= 3
    assert (await rig.parts.state.snapshot()).sent_today == len(rig.gw.sent)


async def test_over_budget_message_is_dropped_not_sent_later(sf: SF, clock: FakeClock) -> None:
    rig = Rig(sf, clock, budget_per_day=1, evening_valid_h=18)
    await add_task(sf)
    await rig.cycle()  # morning takes the one slot
    clock.set(ist(20, 40))
    await rig.cycle()
    clock.set(ist(21, 30))
    await rig.cycle()
    assert len(rig.gw.sent) == 1


async def test_quiet_hours_defer_then_send_when_over(sf: SF) -> None:
    clock = FakeClock(ist(8, 40))
    rig = Rig(sf, clock, quiet_start="08:00", quiet_end="09:00", evening_enabled=False)
    await add_task(sf)
    await rig.cycle()
    assert rig.gw.sent == []  # quiet: deferred, no attempt burned
    async with sf() as s:
        job = (await s.execute(select(Job))).scalar_one()
    assert job.attempts == 0 and job.status == "pending" and job.run_at == ist(9)
    clock.set(ist(9, 1))
    await rig.cycle()
    assert len(rig.gw.sent) == 1


async def test_message_that_would_land_after_its_validity_is_dropped(sf: SF) -> None:
    clock = FakeClock(ist(8, 40))
    rig = Rig(sf, clock, quiet_start="08:00", quiet_end="22:00", evening_enabled=False)
    await add_task(sf)
    await rig.cycle()
    clock.set(ist(22, 5))
    await rig.cycle()
    assert rig.gw.sent == []


async def test_default_quiet_hours_hold_back_a_late_slot(sf: SF) -> None:
    clock = FakeClock(ist(22, 5))
    rig = Rig(sf, clock, morning_enabled=False, evening_at="21:30", evening_valid_h=3)
    await rig.cycle()
    assert rig.gw.sent == []


async def test_snooze_command_mutes_then_resumes(sf: SF, clock: FakeClock) -> None:
    rig = Rig(sf, clock, evening_enabled=False, morning_valid_h=12)
    await add_task(sf)
    reply = await rig.parts.commands.handle("/snooze 2h")
    assert reply is not None and "10:31 AM IST" in reply
    await rig.cycle()
    assert rig.gw.sent == []
    clock.advance(hours=2, minutes=1)
    await rig.cycle()
    assert len(rig.gw.sent) == 1


async def test_commands_parse_deterministically(sf: SF, clock: FakeClock) -> None:
    cmd = Rig(sf, clock).parts.commands
    assert await cmd.handle("hello there") is None
    assert "Usage" in (await cmd.handle("/snooze banana") or "")
    quiet = await cmd.handle("/quiet")
    assert quiet is not None and "3 Nov 2026, 8:00 AM IST" in quiet
    assert await cmd.handle("/snooze 9999d") is not None
    snap = await ProactiveState(sf, clock).snapshot()
    assert snap.muted_until is not None and snap.muted_until <= clock.now() + timedelta(hours=72)
    assert "back on" in (await cmd.handle("/resume") or "")
    assert (await ProactiveState(sf, clock).snapshot()).muted_until is None


async def test_catch_up_after_restart_sends_at_most_one_missed_brief(sf: SF) -> None:
    clock = FakeClock(ist(21, 0))  # app was down since the morning
    rig = Rig(sf, clock, morning_valid_h=18, evening_valid_h=18)
    await add_task(sf)
    await rig.cycle()
    await rig.cycle()
    assert len(rig.gw.sent) == 1 and rig.gw.sent[0].startswith("Evening")
    rig.parts = rig.build()
    await rig.cycle()
    assert len(rig.gw.sent) == 1


async def test_stale_slot_is_not_sent(sf: SF) -> None:
    clock = FakeClock(ist(16, 0))  # morning (valid 6h) is over; evening not yet
    rig = Rig(sf, clock)
    await add_task(sf)
    await rig.cycle()
    assert rig.gw.sent == []


async def test_evening_check_in_is_short_one_question(sf: SF) -> None:
    clock = FakeClock(ist(20, 31))
    rig = Rig(sf, clock, morning_enabled=False)
    await rig.cycle()
    (text,) = rig.gw.sent
    assert text.count("?") == 1 and len(text.splitlines()) <= 3


async def test_nudge_once_per_commitment_per_day_and_not_for_done_or_far_ones(sf: SF) -> None:
    clock = FakeClock(ist(12, 0))
    rig = Rig(sf, clock, morning_enabled=False, evening_enabled=False, nudge_scan_s=1)
    await rig.confirmed("send deck", "2 Nov 2026 5pm")
    done = await rig.confirmed("ring Bob", "2 Nov 2026 6pm")
    await rig.confirmed("later", "20 Nov 2026 6pm")
    await rig.confirmed("ancient", "29 Oct 2026 11pm")
    cur = await rig.lh.ledger.current(done)
    assert cur is not None
    await rig.lh.ledger.complete(cur.id, event_id=await make_event(sf, "done with Bob"))

    await rig.cycle()
    clock.advance(minutes=5)
    await rig.cycle()
    clock.advance(hours=1)
    rig.parts = rig.build()
    await rig.cycle()
    assert len(rig.gw.sent) == 1
    assert "“send deck”" in rig.gw.sent[0] and rig.gw.sent[0].startswith("Nudge")
    clock.set(ist(12, 0, day=3))
    await rig.cycle()
    assert len(rig.gw.sent) == 2 and "was due" in rig.gw.sent[1]


async def test_nudge_for_a_commitment_completed_after_queueing_is_skipped(sf: SF) -> None:
    clock = FakeClock(ist(12, 0))
    rig = Rig(sf, clock, morning_enabled=False, evening_enabled=False)
    key = await rig.confirmed("send deck", "2 Nov 2026 5pm")
    await rig.parts.scheduler.tick()  # enqueued, not yet run
    cur = await rig.lh.ledger.current(key)
    assert cur is not None
    await rig.lh.ledger.complete(cur.id, event_id=await make_event(sf, "done"))
    await rig.parts.runner.run_due()
    assert rig.gw.sent == []


async def test_weekly_review_counts_and_drains_review_queue_bounded(sf: SF) -> None:
    clock = FakeClock(ist(18, 1))
    rig = Rig(
        sf,
        clock,
        morning_enabled=False,
        evening_enabled=False,
        weekly_weekday=0,
        weekly_cards_max=2,
        nudges_enabled=False,
    )
    rig.review.pending = 5
    await rig.confirmed("send deck", "5 Nov 2026 5pm")
    await rig.cycle()
    (text,) = rig.gw.sent
    assert "Weekly review" in text and "Memory items awaiting your approval: 5" in text
    assert "Commitments open: 1 (1 due in the next 7 days, 0 overdue)" in text
    assert rig.review.drained == [2]


async def test_weekly_not_on_other_weekdays(sf: SF) -> None:
    clock = FakeClock(ist(18, 1))
    rig = Rig(sf, clock, morning_enabled=False, evening_enabled=False, weekly_weekday=5)
    await rig.cycle()
    assert rig.gw.sent == [] and rig.review.drained == []


async def test_failed_send_releases_the_slot_and_retries(sf: SF, clock: FakeClock) -> None:
    rig = Rig(sf, clock, evening_enabled=False)
    await add_task(sf)
    rig.gw.fail = 1
    await rig.cycle()
    assert rig.gw.sent == [] and (await rig.parts.state.snapshot()).sent_today == 0
    clock.advance(minutes=2)  # past the retry backoff
    await rig.cycle()
    assert len(rig.gw.sent) == 1 and (await rig.parts.state.snapshot()).sent_today == 1


async def test_disabled_sends_nothing(sf: SF, clock: FakeClock) -> None:
    rig = Rig(sf, clock, enabled=False)
    await add_task(sf)
    await rig.cycle()
    assert rig.gw.sent == []


async def test_slow_proactive_job_does_not_block_reminders_r11(sf: SF, clock: FakeClock) -> None:
    """R11: the brief's calendar read takes seconds; a reminder due now is
    delivered by its own runner in the meantime."""
    rig = Rig(sf, clock, evening_enabled=False)
    rig.cal = Cal(["x"], delay=1.5)
    rig.parts = rig.build()
    gw_rem = Gw()
    delivery = rem.ReminderDelivery(sf, gw_rem, clock)  # type: ignore[arg-type]
    rem_runner = JobRunner(
        rig.queue, {rem.KIND: delivery.handle}, clock=clock, kinds=frozenset({rem.KIND})
    )
    store = rem.ReminderStore(sf, rig.queue, clock)
    await rig.parts.scheduler.tick()
    slow = asyncio.create_task(rig.parts.runner.run_due())
    await asyncio.sleep(0.3)
    assert not slow.done()
    await store.create("call mom", clock.now())
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await rem_runner.run_due() == 1
    assert loop.time() - t0 < 1.0 and gw_rem.sent == ["Reminder: call mom"]
    assert not slow.done()
    await slow
    assert len(rig.gw.sent) == 1


async def test_reminders_ignore_proactive_mute_and_budget(sf: SF, clock: FakeClock) -> None:
    rig = Rig(sf, clock)
    await rig.parts.commands.handle("/snooze 5h")
    for i in range(7):
        await rig.parts.state.reserve(f"x{i}", "k", budget=6)
    gw = Gw()
    delivery = rem.ReminderDelivery(sf, gw, clock)  # type: ignore[arg-type]
    runner = JobRunner(rig.queue, {rem.KIND: delivery.handle}, clock=clock)
    await rem.ReminderStore(sf, rig.queue, clock).create("alarm", clock.now())
    await runner.run_due()
    assert gw.sent == ["Reminder: alarm"]
