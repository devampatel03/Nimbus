# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Real-Postgres replay/restart tests (R1): no lost and no duplicate updates.

The fake Telegram server keeps unconfirmed updates until a `getUpdates` offset
confirms them (as the real one does). A "restart" is a brand-new
`TelegramGateway` + `UpdateStore` over the same database and the same fake
server. Each test runs in an outer transaction that is rolled back; the
store's own transactions become savepoints."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from src.gateway import InboundUpdate, TelegramGateway, UpdateStore
from src.gateway.store import OFFSET_KEY, Seen
from src.memory.schemas.ops import InboundUpdate as Row
from src.memory.schemas.ops import SystemFlag
from tests.unit.gateway.fake_telegram import (
    FakeTelegram,
    callback,
    make_egress,
    make_settings,
    message,
)


@pytest.fixture
async def sf(pg_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    conn: AsyncConnection
    async with pg_engine.connect() as conn:
        tx = await conn.begin()
        yield async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        await tx.rollback()


async def _no_sleep(_: float) -> None:
    return None


def boot(fake: FakeTelegram, sf: async_sessionmaker[AsyncSession]) -> TelegramGateway:
    return TelegramGateway(make_settings(), make_egress(fake), UpdateStore(sf), sleep=_no_sleep)


async def drain(gw: TelegramGateway, *, ack_until: int | None, stop_after: int) -> list[int]:
    """Consume; ack updates with id <= ack_until; stop after `stop_after` deliveries
    (simulating a crash: the generator is abandoned, not closed cleanly)."""
    seen: list[int] = []
    async for u in gw.inbound():
        seen.append(u.update_id)
        if ack_until is not None and u.update_id <= ack_until:
            await gw.ack(u)
        if len(seen) >= stop_after:
            break
    return seen


async def _rows(sf: async_sessionmaker[AsyncSession]) -> dict[int, bool]:
    async with sf() as s:
        res = await s.execute(select(Row.update_id, Row.processed_at))
        return {uid: done is not None for uid, done in res.all()}


async def test_restart_neither_loses_nor_duplicates(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    base = 9_000_000_000  # keep clear of any real rows in the shared dev DB
    fake = FakeTelegram()
    fake.push(*(message(base + i, f"m{i}") for i in range(1, 6)))

    # Run 1: handle 1 and 2, receive 3 but "crash" before acking it.
    run1 = await drain(boot(fake, sf), ack_until=base + 2, stop_after=3)
    assert run1 == [base + 1, base + 2, base + 3]

    # Run 2 (restart): 3 must come back, 1 and 2 must not.
    run2 = await drain(boot(fake, sf), ack_until=base + 99, stop_after=3)
    assert run2 == [base + 3, base + 4, base + 5]

    # Run 3: nothing left, and Telegram has been told everything is confirmed.
    fake.push(message(base + 6, "late"))
    run3 = await drain(boot(fake, sf), ack_until=base + 99, stop_after=1)
    assert run3 == [base + 6]
    handled = run1[:2] + run2 + run3
    assert sorted(handled) == [base + i for i in range(1, 7)]
    assert len(set(handled)) == len(handled)
    rows = await _rows(sf)
    assert all(rows[base + i] for i in range(1, 7))
    assert await UpdateStore(sf).get_offset() == base + 7


async def test_crash_after_ack_before_next_poll_does_not_replay(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    base = 9_100_000_000
    fake = FakeTelegram()
    fake.push(message(base + 1), message(base + 2))
    await drain(boot(fake, sf), ack_until=base + 1, stop_after=1)
    # Telegram never saw an offset (crash right after ack) -> still holds both.
    assert [u["update_id"] for u in fake.pending] == [base + 1, base + 2]
    again = await drain(boot(fake, sf), ack_until=base + 9, stop_after=1)
    assert again == [base + 2]


async def test_offset_never_passes_an_unprocessed_update(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    base = 9_200_000_000
    store = UpdateStore(sf)
    for i in (1, 2, 3):
        assert await store.begin(base + i, 1, "text") is Seen.NEW
    await store.complete(base + 3)  # 3 done out of order; 1 and 2 still open
    off = await store.get_offset()
    assert off is not None and off <= base + 1
    await store.complete(base + 1)
    assert (await store.get_offset()) == base + 2
    await store.complete(base + 2)
    assert (await store.get_offset()) == base + 4


async def test_offset_is_monotonic_and_idempotent(sf: async_sessionmaker[AsyncSession]) -> None:
    base = 9_300_000_000
    store = UpdateStore(sf)
    await store.record_dropped(base + 10, 5, "dropped")
    await store.record_dropped(base + 10, 5, "dropped")  # duplicate: no error, no change
    assert await store.get_offset() == base + 11
    await store.record_dropped(base + 4, 5, "dropped")  # older id arrives late
    assert await store.get_offset() == base + 11
    async with sf() as s:
        n = await s.scalar(select(func.count()).select_from(Row).where(Row.update_id == base + 10))
    assert n == 1


async def test_begin_is_idempotent_and_reports_state(sf: async_sessionmaker[AsyncSession]) -> None:
    base = 9_400_000_000
    store = UpdateStore(sf)
    assert await store.begin(base, 1, "text") is Seen.NEW
    assert await store.begin(base, 1, "text") is Seen.REDELIVERY
    await store.complete(base)
    assert await store.begin(base, 1, "text") is Seen.DONE


async def test_offset_and_processed_flag_commit_or_roll_back_together(
    sf: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = 9_500_000_000
    store = UpdateStore(sf)
    await store.begin(base, 1, "text")

    async def boom(self: UpdateStore, s: AsyncSession) -> None:
        raise RuntimeError("crash between mark-processed and offset write")

    monkeypatch.setattr(UpdateStore, "_advance", boom)
    with pytest.raises(RuntimeError):
        await store.complete(base)
    monkeypatch.undo()
    # The processed flag rolled back with the failed offset write.
    assert (await _rows(sf))[base] is False
    async with sf() as s:
        flag = await s.get(SystemFlag, OFFSET_KEY)
    assert flag is None or flag.value.get("offset", 0) <= base


async def test_strangers_and_callbacks_replay(sf: async_sessionmaker[AsyncSession]) -> None:
    base = 9_600_000_000
    fake = FakeTelegram()
    fake.push(
        message(base + 1, "evil", user=777),
        callback(base + 2, "ap:x", user=777),
        callback(base + 3, "ap:mine"),
    )
    seen: list[InboundUpdate] = []
    gw = boot(fake, sf)
    async for u in gw.inbound():
        seen.append(u)
        await gw.ack(u)
        await gw.aclose()
    assert [u.update_id for u in seen] == [base + 3]
    assert set(fake.methods()) == {"getUpdates"}
    rows = await _rows(sf)
    assert rows[base + 1] and rows[base + 2] and rows[base + 3]


async def test_poison_budget_survives_restarts_and_unblocks_queue(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    base = 9_700_000_000
    fake = FakeTelegram()
    fake.push(*(message(base + i, "m") for i in range(1, 151)))
    delivered: list[int] = []
    # Each "process" gets exactly one delivery of the poison update, then crashes.
    for _ in range(3):
        gw = TelegramGateway(
            make_settings(), make_egress(fake), UpdateStore(sf, max_deliveries=4), sleep=_no_sleep
        )
        async for u in gw.inbound():
            delivered.append(u.update_id)
            break
    assert delivered == [base + 1] * 3  # budget counted across restarts
    gw = TelegramGateway(
        make_settings(), make_egress(fake), UpdateStore(sf, max_deliveries=4), sleep=_no_sleep
    )
    got: list[int] = []
    async for u in gw.inbound():
        if u.update_id == base + 1:
            got.append(u.update_id)  # 4th delivery: still within budget (never acked)
            continue
        got.append(u.update_id)
        await gw.ack(u)
        if u.update_id == base + 150:
            await gw.aclose()
    assert got.count(base + 1) == 1  # 3 earlier runs + this = 4 = budget
    assert {base + i for i in range(2, 151)} <= set(got)
    assert (await _rows(sf))[base + 1] is True  # poisoned: processed, offset moved on
    assert await UpdateStore(sf).get_offset() == base + 151
