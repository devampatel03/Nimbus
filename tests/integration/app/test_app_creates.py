# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.3-enable in the running process: with the operator opt-in
(`POLICY_ENABLE_CREATES=true`) the model can create reminders and tasks in a
clean turn; a tainted turn still needs an approval; the default stays denied.
Creation is idempotent across a redelivered update and a model that calls the
create twice, and a created reminder fires through the real job runner."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.app.policy_settings import PolicySettings
from src.app.runtime import AppConfig
from src.llm.types import ChatRequest, ProviderResponse, ToolCallRequest, Usage
from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.ops import Job
from src.memory.schemas.scheduling import Reminder, Task
from tests.integration.app.harness import PacedTelegram, backends_for, make_cfg, run_until
from tests.unit.gateway.fake_telegram import message

BASE = 7_200_000_000
IST = timezone(timedelta(hours=5, minutes=30))
SF = async_sessionmaker[AsyncSession]


@pytest.fixture
def sf(app_engine: AsyncEngine) -> SF:
    return async_sessionmaker(app_engine, expire_on_commit=False)


def opted_in(tmp_path: Path) -> AppConfig:
    on = PolicySettings(_env_file=None, enable_creates=True)  # type: ignore[call-arg]
    return dataclasses.replace(make_cfg(tmp_path), policy=on)


class ToolThenText:
    """Model stand-in that obeys: for every user turn it asks for `calls` (a fresh
    sampling each time, so a re-run turn asks again); after a tool result it
    answers with text. Background structured calls answer `{}`."""

    name = "tool-then-text"
    model = "fake"

    def __init__(self, *calls: tuple[str, dict[str, Any]]) -> None:
        self.calls = calls
        self.turns = 0
        self.tool_results: list[str] = []

    async def complete(self, req: ChatRequest) -> ProviderResponse:
        usage = Usage(tokens_in=10, tokens_out=5)
        if req.response_schema is not None:
            return ProviderResponse(text="{}", usage=usage)
        last = req.messages[-1]
        if last.role == "tool":
            self.tool_results.extend(m.content or "" for m in req.messages if m.role == "tool")
            return ProviderResponse(text="Done.", usage=usage)
        self.turns += 1
        reqs = tuple(
            ToolCallRequest(id=f"c{self.turns}_{i}", name=n, arguments=a)
            for i, (n, a) in enumerate(self.calls)
        )
        return ProviderResponse(text=None, tool_calls=reqs, usage=usage)


async def _event(sf: SF, update_id: int) -> EventRow | None:
    async with sf() as s:
        row: EventRow | None = await s.scalar(
            select(EventRow).where(EventRow.source_key == f"telegram:{update_id}")
        )
        return row


async def _reminders(sf: SF, title: str) -> list[Reminder]:
    async with sf() as s:
        return list(await s.scalars(select(Reminder).where(Reminder.title.ilike(title))))


async def _tasks(sf: SF, title: str) -> list[Task]:
    async with sf() as s:
        return list(await s.scalars(select(Task).where(Task.title.ilike(title))))


def _tomorrow_9am_ist(at: datetime) -> datetime:
    day = at.astimezone(IST).date() + timedelta(days=1)
    return datetime.combine(day, time(9, 0), tzinfo=IST)


async def test_remind_me_tomorrow_9am_creates_the_ist_reminder_and_it_fires(
    tmp_path: Path, sf: SF
) -> None:
    title = f"call mom {uuid.uuid4().hex[:6]}"
    model = ToolThenText(("reminders_create", {"title": title, "remind_at": "tomorrow 9am"}))
    fake = PacedTelegram()
    fake.push(message(BASE + 1, "remind me tomorrow 9am to call mom"))
    cfg = opted_in(tmp_path)
    await run_until(cfg, backends_for(fake, model), lambda: bool(fake.sent))
    assert fake.replies() == ["Done."]
    assert model.tool_results and not model.tool_results[0].startswith("Denied")
    ev = await _event(sf, BASE + 1)
    assert ev is not None
    rows = await _reminders(sf, title)
    assert len(rows) == 1
    r = rows[0]
    assert r.remind_at == _tomorrow_9am_ist(ev.occurred_at)
    assert r.source_event_id == ev.id and r.status == "pending"  # M5 stamp
    async with sf() as s:
        jobs = list(await s.scalars(select(Job).where(Job.kind == "reminder_fire")))
    mine = [j for j in jobs if j.payload.get("reminder_id") == str(r.id)]
    assert len(mine) == 1 and mine[0].run_at == r.remind_at
    assert mine[0].source_event_id == ev.id

    # Time-travel the job only (payload unchanged) so the real runner delivers it now.
    async with sf() as s, s.begin():
        await s.execute(update(Job).where(Job.id == mine[0].id).values(run_at=datetime.now(IST)))
    fake2 = PacedTelegram()
    await run_until(
        cfg,
        backends_for(fake2, model),
        lambda: any(t == f"Reminder: {title}" for t in fake2.replies()),
    )
    assert [t for t in fake2.replies() if title in t] == [f"Reminder: {title}"]
    fired = await _reminders(sf, title)
    assert fired[0].status == "fired"


async def test_redelivered_update_and_double_create_make_one_reminder(
    tmp_path: Path, sf: SF
) -> None:
    title = f"pay rent {uuid.uuid4().hex[:6]}"
    args = {"title": title, "remind_at": "friday 6pm"}
    # The model asks twice in one turn, the second time with other case/spacing.
    model = ToolThenText(
        ("reminders_create", args),
        ("reminders_create", {**args, "title": "  " + title.upper() + " "}),
    )
    fake = PacedTelegram()
    msg = message(BASE + 2, "remind me friday 6pm to pay rent")
    fake.push(msg, msg)  # Telegram redelivers the same update
    cfg = opted_in(tmp_path)
    await run_until(cfg, backends_for(fake, model), lambda: bool(fake.sent))
    # A restart where Telegram still holds the update: it must not create again.
    fake2 = PacedTelegram()
    fake2.push(msg)
    await run_until(cfg, backends_for(fake2, model), lambda: not fake2.pending, timeout=10)
    assert len(await _reminders(sf, title)) == 1
    async with sf() as s:
        jobs = list(await s.scalars(select(Job).where(Job.kind == "reminder_fire")))
    rid = str((await _reminders(sf, title))[0].id)
    assert len([j for j in jobs if j.payload.get("reminder_id") == rid]) == 1


async def test_clean_turn_task_create_runs_once(tmp_path: Path, sf: SF) -> None:
    title = f"buy milk {uuid.uuid4().hex[:6]}"
    model = ToolThenText(("tasks_create", {"title": title}), ("tasks_create", {"title": title}))
    fake = PacedTelegram()
    fake.push(message(BASE + 3, "add a task: buy milk"))
    await run_until(opted_in(tmp_path), backends_for(fake, model), lambda: bool(fake.sent))
    rows = await _tasks(sf, title)
    ev = await _event(sf, BASE + 3)
    assert len(rows) == 1 and ev is not None and rows[0].source_event_id == ev.id


async def test_tainted_turn_create_needs_approval_even_when_opted_in(
    tmp_path: Path, sf: SF
) -> None:
    """tasks_list output is untrusted: the later create must not run
    (approval.write_internal_tainted; approvals are off in this harness)."""
    title = f"injected {uuid.uuid4().hex[:6]}"
    model = _Sequenced(
        [("tasks_list", {})],
        [
            ("tasks_create", {"title": title}),
            ("reminders_create", {"title": title, "remind_at": "in 2 hours"}),
        ],
    )
    fake = PacedTelegram()
    fake.push(message(BASE + 4, "look at my tasks"))
    await run_until(opted_in(tmp_path), backends_for(fake, model), lambda: bool(fake.sent))
    denied = [t for t in model.tool_results if "approval.write_internal_tainted" in t]
    assert len(denied) == 2
    assert await _tasks(sf, title) == [] and await _reminders(sf, title) == []


async def test_default_config_still_denies_creates(tmp_path: Path, sf: SF) -> None:
    title = f"nope {uuid.uuid4().hex[:6]}"
    model = ToolThenText(("tasks_create", {"title": title}))
    fake = PacedTelegram()
    fake.push(message(BASE + 5, "add a task"))
    await run_until(make_cfg(tmp_path), backends_for(fake, model), lambda: bool(fake.sent))
    assert any("deny.tool_disabled" in t for t in model.tool_results)
    assert await _tasks(sf, title) == []


@pytest.mark.parametrize("enabled", [False, True])
async def test_boot_logs_the_create_policy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, enabled: bool
) -> None:
    pol = PolicySettings(_env_file=None, enable_creates=enabled)  # type: ignore[call-arg]
    cfg = dataclasses.replace(make_cfg(tmp_path), policy=pol)
    fake = PacedTelegram()
    with caplog.at_level(logging.INFO, logger="src.app.runtime"):
        await run_until(cfg, backends_for(fake), lambda: bool(fake.requests))
    lines = [r for r in caplog.records if "policy creates" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert ("creates enabled" in msg) is enabled and ("creates disabled" in msg) is not enabled


class _Sequenced(ToolThenText):
    """First model call asks for `first`, the next for `second`, then text."""

    def __init__(
        self, first: list[tuple[str, dict[str, Any]]], second: list[tuple[str, dict[str, Any]]]
    ) -> None:
        super().__init__()
        self.steps = [first, second]

    async def complete(self, req: ChatRequest) -> ProviderResponse:
        usage = Usage(tokens_in=10, tokens_out=5)
        if req.response_schema is not None:
            return ProviderResponse(text="{}", usage=usage)
        if not self.steps:
            self.tool_results.extend(m.content or "" for m in req.messages if m.role == "tool")
            return ProviderResponse(text="Done.", usage=usage)
        step = self.steps.pop(0)
        reqs = tuple(
            ToolCallRequest(id=f"s{len(self.steps)}_{i}", name=n, arguments=a)
            for i, (n, a) in enumerate(step)
        )
        return ProviderResponse(text=None, tool_calls=reqs, usage=usage)
