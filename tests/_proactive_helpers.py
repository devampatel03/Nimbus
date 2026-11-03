# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared fakes and the `Rig` for the P5.1 proactive tests (integration and
adversarial): recording gateway, scripted calendar, review queue, ledger harness."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.timeparse import IST
from src.memory.schemas.scheduling import Task
from src.scheduler import proactive as pr
from src.scheduler import proactive_text as tx
from src.scheduler.jobs import JobQueue
from src.scheduler.settings import ProactiveSettings
from tests._ledger_helpers import LedgerHarness, make_event
from tests.fakes.clock import FakeClock

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


class Rig:
    def __init__(self, sf: SF, clock: FakeClock, cal: Any = None, **over: Any) -> None:
        self.sf, self.clock = sf, clock
        self.gw = Gw()
        self.lh = LedgerHarness(sf)
        self.cfg = ProactiveSettings(_env_file=None, **over)  # type: ignore[call-arg]
        self.queue = JobQueue(sf, clock)
        self.review = Review()
        self.cal: Any = cal
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
