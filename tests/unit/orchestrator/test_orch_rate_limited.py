# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P5.5: a rate-limited turn (local limiter or provider 429) gets one fixed,
friendly reply; the raw error never reaches the user. Memory top-k and item
size are configurable cost levers, and top-k 0 skips recall entirely."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from src.llm.fake import FakeLLM
from src.llm.types import LLMProviderError
from src.orchestrator.context import RecalledMemory
from src.orchestrator.dispatch import DenyAllWritesPolicy
from src.orchestrator.loop import (
    RATE_LIMITED_MESSAGE,
    UNAVAILABLE_MESSAGE,
    LoopConfig,
    Orchestrator,
)
from tests.fakes.llm import build_env

CARD = "You are Nimbus."


async def test_local_rate_limit_gives_the_fixed_busy_reply() -> None:
    env = build_env(provider=FakeLLM(default="x"), daily_request_limit=1)
    o = Orchestrator(env.llm, [], DenyAllWritesPolicy(), character_card=CARD)
    assert (await o.run_turn("hi")).status == "ok"
    r = await o.run_turn("hi again")
    assert (r.status, r.text) == ("rate_limited", RATE_LIMITED_MESSAGE)


async def test_provider_429_gives_the_fixed_busy_reply() -> None:
    err = LLMProviderError("rate_limited", status=429, retry_after_s=99.0)
    env = build_env(provider=FakeLLM([err]))
    o = Orchestrator(env.llm, [], DenyAllWritesPolicy(), character_card=CARD)
    r = await o.run_turn("hi")
    assert (r.status, r.text) == ("rate_limited", RATE_LIMITED_MESSAGE)
    assert "429" not in r.text and "quota" not in r.text


async def test_other_provider_errors_keep_the_unavailable_reply() -> None:
    env = build_env(provider=FakeLLM([LLMProviderError("provider_status", status=503)]))
    o = Orchestrator(env.llm, [], DenyAllWritesPolicy(), character_card=CARD)
    r = await o.run_turn("hi")
    assert (r.status, r.text) == ("unavailable", UNAVAILABLE_MESSAGE)


class _Recall:
    def __init__(self) -> None:
        self.calls = 0

    async def recall(self, query: str, *, limit: int) -> Sequence[RecalledMemory]:
        self.calls += 1
        return [RecalledMemory("m" * 900 + "TAIL") for _ in range(limit)]


async def test_memory_top_k_and_item_chars_bound_the_prompt() -> None:
    fake = FakeLLM(default="ok")
    env = build_env(provider=fake)
    recall = _Recall()
    cfg = LoopConfig(memory_top_k=2, memory_item_chars=300)
    o = Orchestrator(
        env.llm, [], DenyAllWritesPolicy(), character_card=CARD, recall=recall, config=cfg
    )
    await o.run_turn("hi")
    system = fake.requests[0].messages[0].content or ""
    assert system.count("m" * 300) == 2 and "m" * 301 not in system and "TAIL" not in system


async def test_memory_top_k_zero_skips_recall() -> None:
    env = build_env(provider=FakeLLM(default="ok"))
    recall = _Recall()
    o = Orchestrator(
        env.llm,
        [],
        DenyAllWritesPolicy(),
        character_card=CARD,
        recall=recall,
        config=LoopConfig(memory_top_k=0),
    )
    await o.run_turn("hi")
    assert recall.calls == 0


@pytest.mark.parametrize("kw", [{"memory_top_k": 21}, {"memory_item_chars": 0}])
def test_memory_limits_are_bounded(kw: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        LoopConfig(**kw)  # type: ignore[arg-type]
