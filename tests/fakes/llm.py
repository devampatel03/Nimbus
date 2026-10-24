# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Test harness for `src.llm`: in-memory stores, a controllable clock, a
`RecordingTransport` (so "zero transport calls" is `transport.calls == []`),
and builders for a `GuardedLLM` over either a `FakeLLM` or the real adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.llm.adapter import OpenAICompatProvider
from src.llm.fake import FakeLLM
from src.llm.guard import GuardedLLM
from src.llm.protocols import Provider
from src.llm.settings import LLMSettings
from src.llm.stores import InMemoryAuditLog, InMemoryFlagStore
from src.net import NetSettings

LLM_URL = "https://llm.example.com/v1"
API_KEY = "sk-test-" + "k" * 24  # pragma: allowlist secret
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def llm_settings(**overrides: Any) -> LLMSettings:
    base: dict[str, Any] = {"base_url": LLM_URL, "api_key": API_KEY}
    base.update(overrides)
    return LLMSettings(_env_file=None, **base)  # type: ignore[call-arg]


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler: Handler | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self._handler is None:
            return completion_response("ok")
        result = self._handler(request)
        if isinstance(result, httpx.Response):
            return result
        return await result


def completion_response(
    text: str | None = "ok",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        # 2026-10-25 10:00 IST
        self.now = start or datetime(2026, 10, 25, 4, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@dataclass
class Alerts:
    messages: list[str] = field(default_factory=list)

    async def __call__(self, message: str) -> None:
        self.messages.append(message)


@dataclass
class Env:
    llm: GuardedLLM
    provider: Provider
    flags: InMemoryFlagStore
    audit: InMemoryAuditLog
    clock: FakeClock
    alerts: Alerts
    transport: RecordingTransport | None = None


async def no_sleep(_: float) -> None:
    return None


def build_env(
    *,
    provider: Provider | None = None,
    transport: RecordingTransport | None = None,
    **settings_overrides: Any,
) -> Env:
    settings = llm_settings(**settings_overrides)
    flags = InMemoryFlagStore()
    audit = InMemoryAuditLog()
    clock = FakeClock()
    alerts = Alerts()
    if provider is None and transport is None:
        provider = FakeLLM(default="fake reply")
    if provider is None:
        provider = OpenAICompatProvider(
            settings,
            net_settings=NetSettings(llm_base_url=LLM_URL),
            transport=transport,
            sleep=no_sleep,
        )
    llm = GuardedLLM(
        provider,
        flags=flags,
        audit=audit,
        ledger=audit,
        settings=settings,
        alert=alerts,
        clock=clock,
    )
    return Env(llm, provider, flags, audit, clock, alerts, transport)
