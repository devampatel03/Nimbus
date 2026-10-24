# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""R5: exceeding the daily cap flips to read-only and alerts; resets at IST
midnight; accounting is from the price table."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.llm.fake import FakeLLM
from src.llm.guard import SPEND_MODE_KEY, ist_day_bounds
from src.llm.pricing import PriceTable
from src.llm.settings import ModelPrice
from src.llm.types import ChatMessage, ChatRequest, Lane, LLMMode, Purpose, SpendCapExceeded
from tests.fakes.llm import RecordingTransport, build_env


def req(text: str = "hi") -> ChatRequest:
    return ChatRequest(
        purpose=Purpose.CHAT,
        lane=Lane.TRUSTED,
        messages=(ChatMessage(role="user", content=text),),
        max_output_tokens=100,
    )


# 100k in / 100k out tokens at 1 INR each per Mtok => 0.2 INR per such call.
PRICE = {"*": ModelPrice(in_per_mtok_inr=Decimal(1), out_per_mtok_inr=Decimal(1))}


async def test_cap_exceeded_goes_read_only_and_alerts_once() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport, daily_spend_cap_inr="0.01", price_table=PRICE)
    # Pre-existing spend of 0.02 today.
    from src.llm.protocols import AuditRecord

    await env.audit.write(
        AuditRecord("chat", "p", "m", "ok", env.clock(), cost_inr=Decimal("0.02"))
    )

    with pytest.raises(SpendCapExceeded):
        await env.llm.chat(req())
    with pytest.raises(SpendCapExceeded):
        await env.llm.chat(req())

    assert transport.calls == []
    assert await env.llm.mode() is LLMMode.READ_ONLY
    assert len(env.alerts.messages) == 1  # alert hook called once, not per refusal
    assert "midnight IST" in env.alerts.messages[0]
    flag = await env.flags.get(SPEND_MODE_KEY)
    assert flag is not None and flag["mode"] == "read_only" and flag["day"] == "2026-10-25"
    assert [r.blocked_reason for r in env.audit.records[1:]] == ["spend_cap", "spend_cap"]


async def test_spending_accumulates_until_the_cap_trips() -> None:
    # each call costs (100 in + 50 out tokens at default settings price) ~ 0.0002 INR
    env = build_env(
        transport=RecordingTransport(),
        daily_spend_cap_inr="0.002",
        price_table={"*": ModelPrice(in_per_mtok_inr=Decimal(10), out_per_mtok_inr=Decimal(10))},
    )
    ok = 0
    with pytest.raises(SpendCapExceeded):
        for _ in range(50):
            await env.llm.chat(req())
            ok += 1
    assert 1 <= ok < 50
    spent = sum(r.cost_inr for r in env.audit.records if r.status == "ok")
    assert spent <= Decimal("0.002")
    assert await env.llm.mode() is LLMMode.READ_ONLY
    assert len(env.alerts.messages) == 1


async def test_read_only_resets_at_ist_midnight() -> None:
    env = build_env(daily_spend_cap_inr="0.01", price_table=PRICE)
    from src.llm.protocols import AuditRecord

    await env.audit.write(AuditRecord("chat", "p", "m", "ok", env.clock(), cost_inr=Decimal(1)))
    with pytest.raises(SpendCapExceeded):
        await env.llm.chat(req())
    assert await env.llm.mode() is LLMMode.READ_ONLY

    # 2026-10-25 10:00 IST -> next day 00:30 IST (= 19:00 UTC on the 25th)
    env.clock.now = datetime(2026, 10, 25, 19, 0, tzinfo=UTC)
    assert await env.llm.mode() is LLMMode.NORMAL
    assert (await env.llm.chat(req())).text == "fake reply"


async def test_alert_hook_failure_does_not_mask_the_refusal() -> None:
    env = build_env(daily_spend_cap_inr="0.01", price_table=PRICE)
    from src.llm.protocols import AuditRecord

    await env.audit.write(AuditRecord("chat", "p", "m", "ok", env.clock(), cost_inr=Decimal(1)))

    async def bad_alert(message: str) -> None:
        raise RuntimeError("telegram down")

    env.llm._alert = bad_alert
    with pytest.raises(SpendCapExceeded):
        await env.llm.chat(req())
    assert await env.llm.mode() is LLMMode.READ_ONLY


async def test_concurrent_calls_cannot_overshoot_via_inflight_reservation() -> None:
    gate = asyncio.Event()

    class Slow(FakeLLM):
        async def complete(self, r: ChatRequest):  # type: ignore[no-untyped-def]
            await gate.wait()
            return await super().complete(r)

    prov = Slow(default="x")
    # estimate per call ~ 100 out tokens * 1000/1e6 = 0.1; cap fits exactly one.
    env = build_env(
        provider=prov,
        daily_spend_cap_inr="0.15",
        price_table={"*": ModelPrice(in_per_mtok_inr=Decimal(0), out_per_mtok_inr=Decimal(1000))},
    )
    t1 = asyncio.create_task(env.llm.chat(req()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    with pytest.raises(SpendCapExceeded):
        await env.llm.chat(req())
    gate.set()
    assert (await t1).text == "x"


def test_pricing_cost_and_unknown_model_never_free() -> None:
    table = PriceTable(
        {"m1": ModelPrice(in_per_mtok_inr=Decimal(25), out_per_mtok_inr=Decimal(100))}
    )
    assert table.cost_inr("m1", 1_000_000, 1_000_000) == Decimal("125.0000")
    assert table.cost_inr("m1", 0, 0) == Decimal("0.0000")
    assert table.cost_inr("unknown", 1000, 1000) > 0  # falls to the pessimistic default
    assert table.estimate_inr("m1", 1000, 800) > table.cost_inr("m1", 0, 800)


def test_ist_day_bounds_cross_utc_date() -> None:
    day, start, end = ist_day_bounds(datetime(2026, 10, 25, 19, 0, tzinfo=UTC))  # 00:30 IST 26th
    assert day == "2026-10-26"
    assert start == datetime(2026, 10, 25, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 10, 26, 18, 30, tzinfo=UTC)
