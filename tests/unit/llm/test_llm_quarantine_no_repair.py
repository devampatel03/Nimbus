# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P3.0 MED-D: the guard makes NO repair retry on the QUARANTINE lane. A
malformed quarantine reply fails closed after exactly one billed, audited call
(the repair retry doubled the worst-case quarantine cost per turn). The trusted
lane keeps its single repair retry."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from src.llm.fake import FakeLLM
from src.llm.types import ChatMessage, ChatRequest, Lane, LLMResponseError, Purpose
from tests.fakes.llm import build_env


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int


def req(lane: Lane, purpose: Purpose) -> ChatRequest:
    return ChatRequest(
        purpose=purpose,
        lane=lane,
        messages=(ChatMessage(role="user", content="data"),),
        response_schema=Answer,
    )


async def test_quarantine_lane_malformed_reply_is_one_call_no_repair() -> None:
    fake = FakeLLM(["not json", '{"x": 1}'])
    env = build_env(provider=fake)
    with pytest.raises(LLMResponseError):
        await env.llm.chat(req(Lane.QUARANTINE, Purpose.QUARANTINE_EXTRACT))
    assert len(fake.requests) == 1
    assert len(env.audit.rows) == 1


async def test_quarantine_lane_valid_reply_still_parses() -> None:
    fake = FakeLLM(['{"x": 7}'])
    env = build_env(provider=fake)
    res = await env.llm.chat(req(Lane.QUARANTINE, Purpose.QUARANTINE_EXTRACT))
    assert res.parsed == Answer(x=7) and len(fake.requests) == 1


async def test_trusted_lane_keeps_one_repair_retry() -> None:
    fake = FakeLLM(["not json", '{"x": 2}'])
    env = build_env(provider=fake)
    res = await env.llm.chat(req(Lane.TRUSTED, Purpose.CHAT))
    assert res.parsed == Answer(x=2) and len(fake.requests) == 2
