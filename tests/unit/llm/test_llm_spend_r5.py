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
        price_table={
            "*": ModelPrice(in_per_mtok_inr=Decimal("0.001"), out_per_mtok_inr=Decimal(1000))
        },
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


async def test_reservation_held_until_audit_row_committed() -> None:
    """R5 regression: a second call arriving while the first call's audit write
    is still in flight must see the first call's cost (reservation or row)."""
    gate = asyncio.Event()
    env = build_env(
        provider=FakeLLM(default="x"),
        daily_spend_cap_inr="1.50",
        price_table={
            "*": ModelPrice(in_per_mtok_inr=Decimal("0.001"), out_per_mtok_inr=Decimal(10000))
        },
    )
    real_write = env.audit.write

    async def slow_write(record):  # type: ignore[no-untyped-def]
        if record.status == "ok":
            await gate.wait()  # audit not yet committed
        return await real_write(record)

    env.audit.write = slow_write  # type: ignore[method-assign]
    # estimate = 100 out tokens * 10000 / 1e6 = 1.00 INR per call; cap 1.50
    t1 = asyncio.create_task(env.llm.chat(req()))
    for _ in range(5):
        await asyncio.sleep(0)  # t1 is now parked inside the audit write
    with pytest.raises(SpendCapExceeded):
        # wait_for: if the cap leaks, call 2 parks on the gate and times out (fails).
        await asyncio.wait_for(env.llm.chat(req()), timeout=2)
    gate.set()
    await t1
    assert sum(r.cost_inr for r in env.audit.records if r.status == "ok") <= Decimal("1.50")


async def test_malformed_responses_after_transport_are_billed_until_cap_trips() -> None:
    from src.llm.types import LLMResponseError
    from tests.fakes.llm import completion_response

    bad = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "not json"}}
    transport = RecordingTransport(
        lambda r: completion_response(
            None, tool_calls=[bad], prompt_tokens=10, completion_tokens=100
        )
    )
    env = build_env(
        transport=transport,
        daily_spend_cap_inr="1.50",
        price_table={
            "*": ModelPrice(in_per_mtok_inr=Decimal("0.001"), out_per_mtok_inr=Decimal(10000))
        },
    )
    outcomes: list[str] = []
    for _ in range(10):
        try:
            await env.llm.chat(req())
        except LLMResponseError:
            outcomes.append("resp_err")
        except SpendCapExceeded:
            outcomes.append("cap")
    assert "cap" in outcomes
    assert len(transport.calls) <= 3  # 1.00 INR billed per errored call, cap 1.50
    errored = [r for r in env.audit.records if r.status == "error"]
    assert errored and all(r.cost_inr > 0 for r in errored)
    assert all(r.tokens_out == 100 for r in errored)


async def test_pre_transport_blocks_are_not_billed() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport, price_table=PRICE)
    await env.flags.set("llm_kill_switch", {"engaged": True}, updated_by="t")
    with pytest.raises(Exception):  # noqa: B017
        await env.llm.chat(req())
    assert transport.calls == []
    assert all(r.cost_inr == 0 for r in env.audit.records)


async def test_audit_write_failure_keeps_reservation_so_cap_still_trips() -> None:
    from src.llm.types import LLMAuditError

    prov = FakeLLM(default="x")
    env = build_env(
        provider=prov,
        daily_spend_cap_inr="1.50",
        price_table={
            "*": ModelPrice(in_per_mtok_inr=Decimal("0.001"), out_per_mtok_inr=Decimal(10000))
        },
    )
    real = env.audit.write

    async def write(rec):  # type: ignore[no-untyped-def]
        if rec.status == "ok":
            raise OSError("disk full")
        return await real(rec)

    env.audit.write = write  # type: ignore[method-assign]
    outcomes: list[str] = []
    for _ in range(10):
        try:
            await env.llm.chat(req())
        except LLMAuditError:
            outcomes.append("audit_err")
        except SpendCapExceeded:
            outcomes.append("cap")
    assert "cap" in outcomes
    assert outcomes.count("audit_err") <= 3


_BILLED = {
    "*": ModelPrice(in_per_mtok_inr=Decimal("0.001"), out_per_mtok_inr=Decimal(10000))
}  # estimate for req() ~= 1.00 INR


async def test_timeouts_after_send_are_billed_per_send_and_trip_cap() -> None:
    import httpx

    from src.llm.types import LLMProviderError

    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    t = RecordingTransport(handler)
    env = build_env(transport=t, daily_spend_cap_inr="1.50", price_table=_BILLED)
    outcomes: list[str] = []
    for _ in range(10):
        try:
            await env.llm.chat(req())
        except LLMProviderError:
            outcomes.append("err")
        except SpendCapExceeded:
            outcomes.append("cap")
    assert "cap" in outcomes
    assert len(t.calls) <= 2  # ceil(1.50 / 1.00)
    errored = [r for r in env.audit.records if r.status == "error"]
    assert errored and all(r.cost_inr > 0 for r in errored)


async def test_provider_4xx_refusal_is_not_billed() -> None:
    import httpx

    from src.llm.types import LLMProviderError

    t = RecordingTransport(lambda r: httpx.Response(401, json={"error": "x"}))
    env = build_env(transport=t, price_table=_BILLED)
    with pytest.raises(LLMProviderError):
        await env.llm.chat(req())
    assert all(r.cost_inr == 0 for r in env.audit.records)


async def test_failing_error_row_write_holds_reservation() -> None:
    from src.llm.types import LLMResponseError
    from tests.fakes.llm import completion_response

    bad = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "nope"}}
    t = RecordingTransport(
        lambda r: completion_response(
            None, tool_calls=[bad], prompt_tokens=10, completion_tokens=100
        )
    )
    env = build_env(transport=t, daily_spend_cap_inr="1.50", price_table=_BILLED)
    real = env.audit.write

    async def write(rec):  # type: ignore[no-untyped-def]
        if rec.status == "error":
            raise OSError("disk full")
        return await real(rec)

    env.audit.write = write  # type: ignore[method-assign]
    outcomes: list[str] = []
    for _ in range(10):
        try:
            await env.llm.chat(req())
        except LLMResponseError:
            outcomes.append("err")
        except SpendCapExceeded:
            outcomes.append("cap")
    assert "cap" in outcomes
    assert len(t.calls) <= 2


async def test_cancelled_in_flight_calls_are_billed() -> None:
    from tests.fakes.llm import completion_response

    gate = asyncio.Event()

    async def slow(r):  # type: ignore[no-untyped-def]
        await gate.wait()
        return completion_response("ok")

    t = RecordingTransport(slow)
    env = build_env(transport=t, daily_spend_cap_inr="1.50", price_table=_BILLED)
    task = asyncio.create_task(env.llm.chat(req()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    errored = [r for r in env.audit.records if r.status == "error"]
    assert len(errored) == 1 and all(r.cost_inr > 0 for r in errored)
    with pytest.raises(SpendCapExceeded):
        await env.llm.chat(req())


async def _cancel_during_write(slow_status: str, handler) -> None:  # type: ignore[no-untyped-def]
    t = RecordingTransport(handler)
    env = build_env(transport=t, daily_spend_cap_inr="1.50", price_table=_BILLED)
    real = env.audit.write

    async def w(rec):  # type: ignore[no-untyped-def]
        if rec.status == slow_status:
            await asyncio.sleep(10)
        return await real(rec)

    env.audit.write = w  # type: ignore[method-assign]
    capped = False
    for _ in range(6):
        n0 = len(t.calls)
        task = asyncio.create_task(env.llm.chat(req()))
        for _i in range(200):
            if len(t.calls) > n0 or task.done():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except SpendCapExceeded:
            capped = True
            break
    assert capped
    assert len(t.calls) <= 2  # ceil(1.50 / 1.00)


async def test_cancel_during_ok_row_write_holds_reservation() -> None:
    from tests.fakes.llm import completion_response

    await _cancel_during_write(
        "ok", lambda q: completion_response("ok", prompt_tokens=10, completion_tokens=100)
    )


async def test_cancel_during_error_row_write_holds_reservation() -> None:
    from tests.fakes.llm import completion_response

    bad = {"id": "c", "type": "function", "function": {"name": "t", "arguments": "nope"}}
    await _cancel_during_write(
        "error",
        lambda q: completion_response(
            None, tool_calls=[bad], prompt_tokens=10, completion_tokens=100
        ),
    )


@pytest.mark.parametrize("status", [504, 408])
async def test_gateway_timeout_statuses_are_billed_and_not_retried(status: int) -> None:
    import httpx

    from src.llm.types import LLMProviderError

    t = RecordingTransport(lambda r: httpx.Response(status, json={"error": "x"}))
    env = build_env(transport=t, daily_spend_cap_inr="1.50", price_table=_BILLED)
    with pytest.raises(LLMProviderError):
        await env.llm.chat(req())
    assert len(t.calls) == 1
    assert env.audit.records[0].cost_inr > 0
