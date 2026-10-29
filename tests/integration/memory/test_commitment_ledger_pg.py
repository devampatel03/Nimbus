# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.9 on real Postgres: "I'll send X by Friday" -> CANDIDATE -> card -> the
user's genuine tap (user-bound, single-use) -> confirmed ledger row. Append-only
chain; idempotent; untrusted-origin stays a candidate until approved."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.approvals import service as svc
from src.memory.commitments import LedgerRefused
from tests._approval_helpers import isolated_schema
from tests._ledger_helpers import LedgerHarness, make_event

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with isolated_schema() as (_, factory):
        yield factory


@pytest.fixture
def lh(sf: async_sessionmaker[AsyncSession]) -> LedgerHarness:
    return LedgerHarness(sf)


async def rows(lh: LedgerHarness, key: uuid.UUID) -> list[tuple[int, str]]:
    got = await lh.h.rows(
        "SELECT version, status FROM commitment_ledger WHERE commitment_key = :k ORDER BY version",
        k=key,
    )
    return [(int(r[0]), str(r[1])) for r in got]


async def test_message_yields_candidate_then_user_tap_confirms(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    (cand,) = await lh.intake.on_extracted(
        ev, [("send the budget deck to Priya", 0.9, "by Friday")]
    )
    assert cand.status == "candidate" and cand.lane == "trusted"
    assert cand.due_at == datetime(2026, 10, 30, 3, 30, tzinfo=UTC)
    assert await rows(lh, cand.key) == [(1, "candidate")]
    assert await lh.ledger.open_confirmed() == []  # nothing confirmed yet
    assert [c.id for c in await lh.ledger.review_queue()] == [cand.id]

    card = lh.h.last_card()
    assert "send the budget deck to Priya" in card.text
    assert "Fri 30 Oct 2026, 9:00 AM IST" in card.text
    assert "Tool: commitment" in card.text and "Action: confirm" in card.text

    assert await lh.h.tap(card.approve_data) == "Approved. Running it now."
    assert await rows(lh, cand.key) == [(1, "candidate"), (2, "confirmed")]
    now = await lh.ledger.current(cand.key)
    assert now is not None and now.status == "confirmed" and now.due_at == cand.due_at
    assert [c.key for c in await lh.ledger.open_confirmed()] == [cand.key]
    assert await lh.ledger.review_queue() == []

    # Replay / double tap / reject afterwards: still exactly one confirmation.
    for data in (card.approve_data, card.reject_data):
        assert await lh.h.tap(data) == svc.DONE_ALREADY
    assert await rows(lh, cand.key) == [(1, "candidate"), (2, "confirmed")]


async def test_reject_tap_confirms_nothing(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    (cand,) = await lh.intake.on_extracted(ev, [("send the deck", 0.9, None)])
    card = lh.h.last_card()
    assert await lh.h.tap(card.reject_data) == svc.REJECTED
    assert await lh.h.tap(card.approve_data) == svc.DONE_ALREADY
    assert await rows(lh, cand.key) == [(1, "candidate"), (2, "rejected")]  # Reject recorded


async def test_unresolved_due_is_asked_not_guessed(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf, "I'll send it 5/6")
    (cand,) = await lh.intake.on_extracted(ev, [("send it", 0.9, "5/6")])
    assert cand.due_at is None and cand.due_state == "unresolved"
    assert "NOT SET" in lh.h.last_card().text
    # The user answers; a NEW candidate version carries the resolved time, still unconfirmed.
    ev2 = await make_event(lh.sf, "by 7 Nov 2026 5pm")
    v2 = await lh.ledger.set_due(cand.id, "7 Nov 2026 5pm", event_id=ev2)
    assert v2.version == 2 and v2.status == "candidate" and v2.due_at is not None
    assert await rows(lh, cand.key) == [(1, "candidate"), (2, "candidate")]
    with pytest.raises(LedgerRefused):  # v1 is superseded: cannot be confirmed any more
        await lh.ledger.confirm(cand.id, uuid.uuid4())


async def test_ambiguous_set_due_answer_is_refused(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf, "I'll send it soon")
    (cand,) = await lh.intake.on_extracted(ev, [("send it", 0.9, "soon")])
    ev2 = await make_event(lh.sf, "5/6")
    with pytest.raises(LedgerRefused):
        await lh.ledger.set_due(cand.id, "5/6", event_id=ev2)


async def test_model_supplied_date_is_ignored_and_forged_phrase_unresolved(
    lh: LedgerHarness,
) -> None:
    ev = await make_event(lh.sf)
    (cand,) = await lh.intake.on_extracted(ev, [("send the deck", 0.9, "tomorrow at 1am")])
    assert cand.due_at is None and cand.due_state == "unresolved"


async def test_duplicate_and_replay_are_idempotent(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    first = await lh.intake.on_extracted(ev, [("send the deck", 0.9, "by Friday")])
    assert len(first) == 1
    # Same event replayed (job retry) and same commitment restated in another message.
    assert await lh.intake.on_extracted(ev, [("Send the deck!", 0.9, "by Friday")]) == []
    ev2 = await make_event(lh.sf)
    assert await lh.intake.on_extracted(ev2, [("send  the DECK", 0.8, None)]) == []
    (n,) = (await lh.h.rows("SELECT count(*) FROM commitment_ledger"))[0]
    assert n == 1
    assert len(lh.h.gw.cards) == 1  # one card, not three


async def test_weak_or_empty_extractions_are_dropped(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    out = await lh.intake.on_extracted(ev, [("send the deck", 0.2, None), ("   ", 0.9, None)])
    assert out == []


async def test_untrusted_event_lane_is_inherited_and_needs_the_same_approval(
    lh: LedgerHarness,
) -> None:
    ev = await make_event(lh.sf, "Priya: I will pay by Friday", lane="untrusted", kind="forward")
    (cand,) = await lh.intake.on_extracted(ev, [("Priya will pay", 0.9, "by Friday")])
    assert cand.lane == "untrusted"
    assert "UNTRUSTED" in lh.h.last_card().text
    assert await lh.ledger.open_confirmed() == []
    # Only a trusted user event may reject/close; an untrusted one cannot.
    with pytest.raises(LedgerRefused):
        await lh.ledger.reject(cand.id, event_id=ev)
    await lh.h.tap(lh.h.last_card().approve_data)
    got = await lh.h.rows(
        "SELECT lane, promoted, promoted_approval_id IS NOT NULL FROM commitment_ledger "
        "WHERE commitment_key = :k AND status = 'confirmed'",
        k=cand.key,
    )
    assert [tuple(r) for r in got] == [("untrusted", True, True)]


async def test_lifecycle_and_hooks(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    (cand,) = await lh.intake.on_extracted(ev, [("send the deck", 0.9, "by Friday")])
    await lh.h.tap(lh.h.last_card().approve_data)
    done_ev = await make_event(lh.sf, "sent the deck")
    cur = await lh.ledger.current(cand.key)
    assert cur is not None
    due_soon = await lh.ledger.open_confirmed(due_before=datetime(2026, 10, 31, tzinfo=UTC))
    assert [c.key for c in due_soon] == [cand.key]
    assert await lh.ledger.open_confirmed(due_before=datetime(2026, 10, 1, tzinfo=UTC)) == []
    closed = await lh.ledger.complete(cur.id, event_id=done_ev)
    assert closed.status == "completed" and closed.version == 3
    assert await lh.ledger.open_confirmed() == []
    with pytest.raises(LedgerRefused):
        await lh.ledger.cancel(cur.id, event_id=done_ev)  # superseded row
    with pytest.raises(LedgerRefused):
        await lh.ledger.cancel(closed.id, event_id=done_ev)  # completed is terminal


async def test_rows_are_immutable_in_the_database(lh: LedgerHarness) -> None:
    ev = await make_event(lh.sf)
    (cand,) = await lh.intake.on_extracted(ev, [("send the deck", 0.9, None)])
    for sql in (
        "UPDATE commitment_ledger SET status = 'confirmed'",
        "UPDATE commitment_ledger SET description = 'x'",
        "DELETE FROM commitment_ledger",
        "TRUNCATE commitment_ledger",
    ):
        async with lh.sf() as s:
            with pytest.raises(DBAPIError):
                await s.execute(text(sql))
                await s.commit()
    assert (await lh.ledger.get(cand.id)) is not None
