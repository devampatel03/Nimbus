# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""M5: reminder and task creation is idempotent per turn. The stored event's id
(code-derived, via `turn_event`) is stamped as `source_event_id`; replaying the
same turn creates nothing new, and a database index backs that up."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.turn_context import turn_event
from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.ops import Job
from src.memory.schemas.scheduling import Reminder, Task
from src.scheduler.jobs import JobQueue
from src.scheduler.reminders import ReminderStore
from src.tools.integrations.reminders import RemindersTool
from src.tools.integrations.tasks import TasksTool
from tests.fakes.clock import FakeClock

SF = async_sessionmaker[AsyncSession]


async def make_event(sf: SF) -> uuid.UUID:
    async with sf() as s, s.begin():
        e = EventRow(
            source_key=f"t:{uuid.uuid4()}",
            kind="text",
            source="telegram",
            lane="trusted",
            body="x",
            meta={},
            occurred_at=FakeClock().now(),
        )
        s.add(e)
        await s.flush()
        return e.id


def rem_tools(sf: SF, clock: FakeClock) -> dict[str, object]:
    g = RemindersTool()
    g.bind(ReminderStore(sf, JobQueue(sf, clock), clock), clock)
    return {t.spec.name: t for t in g.tools()}


def task_tools(sf: SF, clock: FakeClock) -> dict[str, object]:
    g = TasksTool()
    g.bind(sf, clock)
    return {t.spec.name: t for t in g.tools()}


async def count(sf: SF, model: type) -> int:
    async with sf() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


ARGS = {"title": "call the dentist", "remind_at": "in 2 hours"}


async def test_replayed_turn_creates_one_reminder_and_one_job(sf: SF, clock: FakeClock) -> None:
    eid = await make_event(sf)
    tools = rem_tools(sf, clock)
    with turn_event(eid):
        a = json.loads((await tools["reminders_create"].run(ARGS)).text)  # type: ignore[attr-defined]
        b = json.loads((await tools["reminders_create"].run(ARGS)).text)  # type: ignore[attr-defined]
    assert a["reminder_id"] == b["reminder_id"]
    assert await count(sf, Reminder) == 1 and await count(sf, Job) == 1
    async with sf() as s:
        r = (await s.execute(select(Reminder))).scalar_one()
        job = (await s.execute(select(Job))).scalar_one()
    assert r.source_event_id == eid and job.source_event_id == eid


async def test_different_turns_or_times_are_not_merged(sf: SF, clock: FakeClock) -> None:
    e1, e2 = await make_event(sf), await make_event(sf)
    tools = rem_tools(sf, clock)
    with turn_event(e1):
        await tools["reminders_create"].run(ARGS)  # type: ignore[attr-defined]
        await tools["reminders_create"].run({**ARGS, "remind_at": "in 3 hours"})  # type: ignore[attr-defined]
    with turn_event(e2):
        await tools["reminders_create"].run(ARGS)  # type: ignore[attr-defined]
    assert await count(sf, Reminder) == 3 and await count(sf, Job) == 3


async def test_without_a_turn_event_nothing_is_merged(sf: SF, clock: FakeClock) -> None:
    tools = rem_tools(sf, clock)
    await tools["reminders_create"].run(ARGS)  # type: ignore[attr-defined]
    await tools["reminders_create"].run(ARGS)  # type: ignore[attr-defined]
    assert await count(sf, Reminder) == 2


async def test_replayed_turn_creates_one_task(sf: SF, clock: FakeClock) -> None:
    eid = await make_event(sf)
    tools = task_tools(sf, clock)
    with turn_event(eid):
        a = json.loads((await tools["tasks_create"].run({"title": "buy milk"})).text)  # type: ignore[attr-defined]
        b = json.loads((await tools["tasks_create"].run({"title": "buy milk"})).text)  # type: ignore[attr-defined]
        await tools["tasks_create"].run({"title": "buy bread"})  # type: ignore[attr-defined]
    assert a["task_id"] == b["task_id"]
    assert await count(sf, Task) == 2
    async with sf() as s:
        assert {t.source_event_id for t in await s.scalars(select(Task))} == {eid}


async def test_database_refuses_a_duplicate_even_around_the_store(sf: SF, clock: FakeClock) -> None:
    eid = await make_event(sf)
    when = clock.now() + timedelta(days=1)
    for model, extra in ((Reminder, {"remind_at": when}), (Task, {})):
        async with sf() as s, s.begin():
            s.add(model(title="dup", source_event_id=eid, **extra))
        with pytest.raises(IntegrityError):
            async with sf() as s, s.begin():
                s.add(model(title="dup", source_event_id=eid, **extra))
    async with sf() as s, s.begin():  # NULL source: the guard does not apply
        s.add(Task(title="dup"))
        s.add(Task(title="dup"))
