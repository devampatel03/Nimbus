# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.9 review follow-ups (MED-1/2/3, LOW-2) on real Postgres: Reject and expiry
are recorded or re-asked, commitment cards have their own cap, a failing ledger
never blocks the episodic chunks. Telegram, the clock and the model are fakes."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.approvals import service as svc
from src.memory.commitments import Commitment, CommitmentIntake
from src.memory.manager import DeterministicStubEmbedder
from src.scheduler.memory_extract import MemoryExtractHandler
from tests._approval_helpers import isolated_schema
from tests._ledger_helpers import T_MSG, LedgerHarness, make_event
from tests.fakes.clock import FakeClock
from tests.integration.memory.test_commitment_extract_pg import ScriptedLLM, ctx

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with isolated_schema() as (_, factory):
        yield factory


@pytest.fixture
def lh(sf: async_sessionmaker[AsyncSession]) -> LedgerHarness:
    return LedgerHarness(sf)


async def statuses(lh: LedgerHarness) -> list[tuple[int, str]]:
    got = await lh.h.rows("SELECT version, status FROM commitment_ledger ORDER BY created_at")
    return [(int(r[0]), str(r[1])) for r in got]


async def test_reject_tap_is_recorded_and_a_restatement_is_asked_again(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    (cand,) = await lh.intake.on_extracted(ev, [("send the deck", 0.9, "by Friday")])
    assert await lh.h.tap(lh.h.last_card().reject_data) == svc.REJECTED
    assert await statuses(lh) == [(1, "candidate"), (2, "rejected")]
    assert await lh.ledger.review_queue() == []  # not invisible, not pending
    assert await lh.ledger.open_confirmed() == []

    ev2 = await make_event(lh.sf)
    (again,) = await lh.intake.on_extracted(ev2, [("send the deck", 0.9, "by Friday")])
    assert again.key != cand.key and len(lh.h.gw.cards) == 2  # a fresh question


async def test_expired_card_is_asked_again_on_restatement_without_a_second_row(
    lh: LedgerHarness,
) -> None:
    ev = await make_event(lh.sf)
    await lh.intake.on_extracted(ev, [("send the deck", 0.9, None)])
    lh.h.clock.advance(60 * 60)  # card lapses; nobody taps it
    ev2 = await make_event(lh.sf)
    assert await lh.intake.on_extracted(ev2, [("Send the deck!", 0.9, None)]) == []
    assert len(lh.h.gw.cards) == 2 and await statuses(lh) == [(1, "candidate")]
    # The same event replayed while a card is open stays a no-op.
    assert await lh.intake.on_extracted(ev2, [("send the deck", 0.9, None)]) == []
    assert len(lh.h.gw.cards) == 2


async def test_commitment_cards_have_their_own_cap_and_queue(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    made = await lh.intake.on_extracted(ev, [("send the deck", 0.9, None), ("pay rent", 0.9, None)])
    assert len(made) == 2 and len(lh.h.gw.cards) == 1  # one card, one queued
    assert len(await lh.ledger.review_queue()) == 2
    # Tool approvals keep their slots (max_open 3, commitments use at most 1).
    await lh.h.propose("tasks_update", {"task_id": "1"})
    assert len(lh.h.gw.cards) == 2
    # Closing the first card asks the queued one.
    await lh.h.tap(lh.h.gw.cards[0].reject_data)
    assert any("pay rent" in c.text for c in lh.h.gw.cards)
    assert await statuses(lh) == [(1, "candidate"), (1, "candidate"), (2, "rejected")]


async def test_unsent_card_is_logged_not_silent(
    lh: LedgerHarness, caplog: pytest.LogCaptureFixture
) -> None:
    ev = await make_event(lh.sf)
    with caplog.at_level(logging.INFO):
        await lh.intake.on_extracted(ev, [("send the deck", 0.9, None), ("pay rent", 0.9, None)])
    assert "outcome=too_many" in caplog.text


async def test_card_shows_the_default_time_assumption(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    await lh.intake.on_extracted(ev, [("send the deck", 0.9, "by Friday")])
    assert "No time given, used 9:00 AM" in lh.h.last_card().text
    lh.h.clock.advance(60 * 60)
    ev2 = await make_event(lh.sf)
    await lh.intake.on_extracted(ev2, [("send the deck", 0.9, None)])  # re-ask reads the same
    assert "No time given, used 9:00 AM" in lh.h.last_card().text


class ExplodingIntake(CommitmentIntake):
    async def on_extracted(  # type: ignore[override]
        self, *a: object, **k: object
    ) -> list[Commitment]:
        raise RuntimeError("secret sk-LEAK untrusted text")


async def test_ledger_failure_neither_blocks_chunks_nor_repeats_the_paid_call(
    lh: LedgerHarness, caplog: pytest.LogCaptureFixture
) -> None:
    llm = ScriptedLLM()
    h = MemoryExtractHandler(lh.sf, DeterministicStubEmbedder(), llm, clock=FakeClock(T_MSG))  # type: ignore[arg-type]
    h.bind_commitments(ExplodingIntake(lh.ledger, lh.h.service))
    ev = await make_event(lh.sf, "I'll send the report by Friday")
    with caplog.at_level(logging.WARNING):
        await h.handle(ctx(ev))  # does not raise: no job retry
    assert llm.calls == 1
    got = await lh.h.rows("SELECT count(*) FROM memory_chunk WHERE source_event_id = :e", e=ev)
    assert got[0][0] >= 2  # the body chunk and the extracted chunk
    assert "RuntimeError" in caplog.text and "LEAK" not in caplog.text
