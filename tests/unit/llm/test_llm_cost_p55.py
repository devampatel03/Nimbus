# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P5.5 (ADR-0025): cost-minimal, provider-agnostic configuration.

Spend cap bounded at 100 INR; prices and a cheap model from env; per-lane
output caps clamped in the guard; request limiter (RPM, daily, background
yields to chat, 429 cool-down) as a deterministic pre-send gate that writes a
`blocked` audit row and makes zero provider calls; 429 handling in the adapter.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from src.llm.adapter import OpenAICompatProvider
from src.llm.factory import build_guarded_llm
from src.llm.fake import FakeLLM
from src.llm.guard import GuardedLLM
from src.llm.ratelimit import RequestLimiter
from src.llm.settings import LLMSettings
from src.llm.stores import InMemoryAuditLog, InMemoryFlagStore
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    Lane,
    LLMProviderError,
    LLMRefused,
    Purpose,
    ToolSpec,
)
from src.net import NetSettings
from tests.fakes.llm import (
    LLM_URL,
    FakeClock,
    RecordingTransport,
    build_env,
    completion_response,
    llm_settings,
    no_sleep,
)


def req(purpose: Purpose = Purpose.CHAT, lane: Lane = Lane.TRUSTED, **kw: Any) -> ChatRequest:
    return ChatRequest(
        purpose=purpose,
        lane=lane,
        messages=(ChatMessage(role="user", content="hello"),),
        **kw,
    )


# ---- settings ---------------------------------------------------------------------


def test_spend_cap_default_and_upper_bound() -> None:
    s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.daily_spend_cap_inr <= 100
    LLMSettings(_env_file=None, daily_spend_cap_inr=Decimal(100))  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None, daily_spend_cap_inr=Decimal("100.01"))  # type: ignore[call-arg]


def test_prices_and_cheap_model_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "big")
    monkeypatch.setenv("LLM_MODEL_CHEAP", "small")
    monkeypatch.setenv("LLM_PRICE_IN_PER_MTOK_INR", "9.5")
    monkeypatch.setenv("LLM_PRICE_OUT_PER_MTOK_INR", "38")
    monkeypatch.setenv("LLM_CHEAP_PRICE_IN_PER_MTOK_INR", "0.0001")
    monkeypatch.setenv("LLM_CHEAP_PRICE_OUT_PER_MTOK_INR", "0.0001")
    s = LLMSettings()
    assert s.model_cheap == "small"
    assert s.price_table["big"].in_per_mtok_inr == Decimal("9.5")
    assert s.price_table["small"].out_per_mtok_inr == Decimal("0.0001")
    assert "*" in s.price_table  # unknown models still priced pessimistically


@pytest.mark.parametrize(
    "kw",
    [
        {"price_in_per_mtok_inr": "1"},  # one side only
        {"price_in_per_mtok_inr": "0", "price_out_per_mtok_inr": "1"},  # free is never 0
        {"cheap_price_in_per_mtok_inr": "1", "cheap_price_out_per_mtok_inr": "1"},  # no model
    ],
)
def test_bad_price_settings_are_rejected(kw: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None, **kw)  # type: ignore[arg-type]


# ---- output caps -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lane", "purpose", "field"),
    [
        (Lane.TRUSTED, Purpose.CHAT, "max_output_tokens_trusted"),
        (Lane.QUARANTINE, Purpose.QUARANTINE_EXTRACT, "max_output_tokens_quarantine"),
        (Lane.SYSTEM, Purpose.MEMORY_EXTRACT, "max_output_tokens_system"),
    ],
)
async def test_guard_clamps_output_tokens_per_lane(
    lane: Lane, purpose: Purpose, field: str
) -> None:
    fake = FakeLLM(default="ok")
    env = build_env(provider=fake, **{field: 77})
    await env.llm.chat(req(purpose, lane, max_output_tokens=5000))
    await env.llm.chat(req(purpose, lane, max_output_tokens=10))
    assert [r.max_output_tokens for r in fake.requests] == [77, 10]


# ---- cheap model routing ------------------------------------------------------------


def _guard(main: FakeLLM, cheap: FakeLLM | None, **kw: Any) -> tuple[GuardedLLM, InMemoryAuditLog]:
    audit = InMemoryAuditLog()
    llm = GuardedLLM(
        main,
        flags=InMemoryFlagStore(),
        audit=audit,
        ledger=audit,
        settings=llm_settings(**kw),
        clock=FakeClock(),
        cheap_provider=cheap,
    )
    return llm, audit


@pytest.mark.parametrize(
    ("purpose", "lane"),
    [
        (Purpose.MEMORY_EXTRACT, Lane.SYSTEM),
        (Purpose.QUARANTINE_EXTRACT, Lane.QUARANTINE),
        (Purpose.FRICTION, Lane.SYSTEM),
    ],
)
async def test_cheap_purposes_use_the_cheap_model_and_its_price(
    purpose: Purpose, lane: Lane
) -> None:
    main, cheap = FakeLLM(default="m", model="big"), FakeLLM(default="c", model="small")
    table = {
        "big": {"in_per_mtok_inr": "1000", "out_per_mtok_inr": "1000"},
        "small": {"in_per_mtok_inr": "1", "out_per_mtok_inr": "1"},
    }
    llm, audit = _guard(main, cheap, price_table=table)
    await llm.chat(req(purpose, lane))
    assert len(cheap.requests) == 1 and main.requests == []
    (row,) = audit.records
    assert row.model == "small" and row.cost_inr < Decimal("0.01")


async def test_chat_never_uses_the_cheap_model() -> None:
    main, cheap = FakeLLM(default="m", model="big"), FakeLLM(default="c", model="small")
    llm, audit = _guard(main, cheap)
    await llm.chat(req())
    assert len(main.requests) == 1 and cheap.requests == []
    assert audit.records[0].model == "big"


def test_factory_builds_a_cheap_provider_on_the_same_base_url() -> None:
    s = llm_settings(model="big", model_cheap="small")
    llm = build_guarded_llm(s)
    assert llm._provider.model == "big"
    assert llm._cheap is not None and llm._cheap.model == "small"
    assert build_guarded_llm(llm_settings(model="big"))._cheap is None


# ---- request limiter ----------------------------------------------------------------


class SleepingClock(FakeClock):
    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.advance(seconds=s)

    def __init__(self) -> None:
        super().__init__()
        self.slept: list[float] = []


def limited_env(**limits: Any) -> tuple[GuardedLLM, FakeLLM, InMemoryAuditLog, SleepingClock]:
    clock = SleepingClock()
    s = llm_settings(**limits)
    limiter = RequestLimiter(
        rpm=s.rpm_limit,
        daily=s.daily_request_limit,
        background_share=s.background_share,
        max_wait_s=s.rate_limit_max_wait_s,
        clock=clock,
        sleep=clock.sleep,
    )
    fake = FakeLLM(default="ok")
    audit = InMemoryAuditLog()
    llm = GuardedLLM(
        fake,
        flags=InMemoryFlagStore(),
        audit=audit,
        ledger=audit,
        settings=s,
        clock=clock,
        limiter=limiter,
    )
    return llm, fake, audit, clock


async def test_chat_waits_for_an_rpm_slot_within_the_max_wait() -> None:
    llm, fake, _, clock = limited_env(rpm_limit=2, rate_limit_max_wait_s=60)
    for _ in range(3):
        await llm.chat(req())
    assert len(fake.requests) == 3 and clock.slept and sum(clock.slept) <= 60


async def test_chat_is_refused_when_the_wait_exceeds_the_max_and_nothing_is_sent() -> None:
    llm, fake, audit, _ = limited_env(rpm_limit=1, rate_limit_max_wait_s=5)
    await llm.chat(req())
    with pytest.raises(LLMRefused) as ei:
        await llm.chat(req())
    assert ei.value.reason == "rate_limited" and len(fake.requests) == 1
    assert [(r.status, r.blocked_reason) for r in audit.records][-1] == ("blocked", "rate_limited")


async def test_daily_request_limit_refuses_chat() -> None:
    llm, fake, _, _ = limited_env(daily_request_limit=2)
    await llm.chat(req())
    await llm.chat(req())
    with pytest.raises(LLMRefused, match="rate_limited"):
        await llm.chat(req())
    assert len(fake.requests) == 2


async def test_background_yields_its_share_and_never_waits() -> None:
    llm, fake, _, clock = limited_env(rpm_limit=4, background_share=0.5)
    bg = req(Purpose.MEMORY_EXTRACT, Lane.SYSTEM)
    await llm.chat(bg)
    await llm.chat(bg)
    with pytest.raises(LLMRefused, match="rate_limited"):
        await llm.chat(bg)  # background share (2 of 4) used up
    assert clock.slept == []
    await llm.chat(req())  # chat still has headroom
    await llm.chat(req())
    assert len(fake.requests) == 4


async def test_provider_429_cools_down_background_calls() -> None:
    clock = SleepingClock()
    s = llm_settings()
    fake = FakeLLM([LLMProviderError("rate_limited", status=429, retry_after_s=20.0)], default="ok")
    limiter = RequestLimiter(
        rpm=30, daily=100, background_share=0.5, max_wait_s=40, clock=clock, sleep=clock.sleep
    )
    audit = InMemoryAuditLog()
    llm = GuardedLLM(
        fake,
        flags=InMemoryFlagStore(),
        audit=audit,
        ledger=audit,
        settings=s,
        clock=clock,
        limiter=limiter,
    )
    with pytest.raises(LLMProviderError):
        await llm.chat(req())
    with pytest.raises(LLMRefused, match="rate_limited"):
        await llm.chat(req(Purpose.MEMORY_EXTRACT, Lane.SYSTEM))
    assert len(fake.requests) == 1
    # Chat waits out the 20 s cool-down (within its 40 s budget) and goes through.
    await llm.chat(req())
    assert len(fake.requests) == 2 and sum(clock.slept) >= 20


def test_limiter_rejects_bad_limits() -> None:
    with pytest.raises(ValueError):
        RequestLimiter(rpm=0, daily=1, background_share=0.5, max_wait_s=1, clock=FakeClock())


# ---- adapter: 429, tool order, reasoning effort ---------------------------------------


def _429(retry_after: str | None = None) -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after else {}
    return httpx.Response(429, json={"error": {"message": "quota"}}, headers=headers)


def _provider(t: RecordingTransport, **kw: Any) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        llm_settings(**kw),
        net_settings=NetSettings(llm_base_url=LLM_URL),
        transport=t,
        sleep=no_sleep,
    )


async def test_background_429_is_not_retried() -> None:
    t = RecordingTransport(lambda r: _429("3"))
    with pytest.raises(LLMProviderError) as ei:
        await _provider(t).complete(req(Purpose.MEMORY_EXTRACT, Lane.SYSTEM))
    assert ei.value.code == "rate_limited" and ei.value.retry_after_s == 3.0
    assert len(t.calls) == 1


async def test_chat_429_gives_up_when_retry_after_exceeds_the_max_wait() -> None:
    t = RecordingTransport(lambda r: _429("25"))
    with pytest.raises(LLMProviderError) as ei:
        await _provider(t, rate_limit_max_wait_s=10).complete(req())
    assert ei.value.code == "rate_limited" and len(t.calls) == 1


async def test_tools_are_sent_in_a_stable_order_and_reasoning_effort_is_passed() -> None:
    t = RecordingTransport(lambda r: completion_response("ok"))
    tools = (ToolSpec(name="zeta"), ToolSpec(name="alpha"), ToolSpec(name="mid"))
    await _provider(t, reasoning_effort="low").complete(req(tools=tools))
    body = json.loads(t.calls[0].content)
    assert [x["function"]["name"] for x in body["tools"]] == ["alpha", "mid", "zeta"]
    assert body["reasoning_effort"] == "low"
    t2 = RecordingTransport(lambda r: completion_response("ok"))
    await _provider(t2).complete(req())
    assert "reasoning_effort" not in json.loads(t2.calls[0].content)
