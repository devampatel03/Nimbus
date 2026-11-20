# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Harness for app-level tests: real Postgres (per-session test DB), real
gateway/ingest/orchestrator/guard, fake Telegram HTTP boundary, fake model."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from src.app.fakes import DevProvider
from src.app.runtime import AppConfig, BackendFactory, Backends, Beat, SessionFactory, run_app
from src.app.settings import AppSettings
from src.gateway.store import UpdateStore
from src.gateway.telegram import TelegramGateway
from src.llm.protocols import Provider
from src.llm.types import ChatRequest, ProviderResponse, Usage
from src.memory.manager import DeterministicStubEmbedder
from src.net import NetSettings
from src.scheduler.settings import ProactiveSettings
from tests.unit.gateway.fake_telegram import FakeTelegram, make_egress, make_settings

CURRENT_DSN = ""  # set per test by conftest: the module's scratch database
TOKEN = "123456789:AAH_fake_token_for_tests_only_0123456789"


class ChatOnly:
    """Wrap a scripted provider so background structured-output calls (memory
    extraction, P3.3) answer `{}` and do not consume the script meant for turns."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model

    async def complete(self, req: ChatRequest) -> ProviderResponse:
        if req.response_schema is not None:
            return ProviderResponse(text="{}", usage=Usage(tokens_in=20, tokens_out=2))
        result: ProviderResponse = await self.inner.complete(req)
        return result


class PacedTelegram(FakeTelegram):
    """Like the real long poll: an empty `getUpdates` waits instead of spinning."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates") and not self.pending:
            await asyncio.sleep(0.02)
        return await super().handle_async_request(request)

    def replies(self) -> list[str]:
        return [m["text"] for m in self.sent]


class EchoProvider:
    """Model stand-in: answers `re:<user text>`; counts calls."""

    name = "echo"
    model = "echo-model"

    def __init__(self, extract_delay_s: float = 0.0) -> None:
        self.extract_delay_s = extract_delay_s
        self.calls = 0
        self.extract_calls = 0
        self.requests: list[ChatRequest] = []

    async def complete(self, req: ChatRequest) -> ProviderResponse:
        if req.response_schema is not None:
            # Background memory extraction (P3.3): not a conversational turn.
            self.extract_calls += 1
            if self.extract_delay_s:
                await asyncio.sleep(self.extract_delay_s)
            return ProviderResponse(text="{}", usage=Usage(tokens_in=20, tokens_out=2))
        self.calls += 1
        self.requests.append(req)
        last_user = next(m.content for m in reversed(req.messages) if m.role == "user")
        return ProviderResponse(text=f"re:{last_user}", usage=Usage(tokens_in=20, tokens_out=5))


def make_cfg(tmp_path: Path, **app: Any) -> AppConfig:
    defaults: dict[str, Any] = {
        "env": "development",
        "heartbeat_path": tmp_path / "hb.json",
        "backoff_initial_s": 0.01,
        "backoff_max_s": 0.05,
        "recover_interval_s": 0.05,
        "scheduler_tick_s": 0.05,
        "job_poll_s": 0.05,
        "heartbeat_stale_s": 3.0,
        "shutdown_grace_s": 5.0,
    }
    defaults.update(app)
    return AppConfig.build(
        app=AppSettings(_env_file=None, **defaults),
        gateway=make_settings(per_chat_interval_s=0, global_interval_s=0, poll_timeout_s=1),
        llm=_llm(),
        net=NetSettings(_env_file=None),
        dsn=CURRENT_DSN,
        # Wall-clock-independent: no catch-up brief may land in these runs (P5.1).
        proactive=ProactiveSettings(_env_file=None, enabled=False),  # type: ignore[call-arg]
    )


def _llm() -> Any:
    from tests.fakes.llm import llm_settings

    return llm_settings()


async def _tiny_sleep(s: float) -> None:
    await asyncio.sleep(min(s, 0.01))


def backends_for(fake: FakeTelegram, provider: Provider | None = None) -> BackendFactory:
    def factory(cfg: AppConfig, sf: SessionFactory, beat: Beat) -> Backends:
        assert cfg.gateway is not None
        egress = make_egress(fake)
        gw = TelegramGateway(cfg.gateway, egress, UpdateStore(sf), sleep=_tiny_sleep, on_poll=beat)
        return Backends(
            gateway=gw,
            provider=provider or DevProvider(),
            embedder=DeterministicStubEmbedder(),
            closers=[gw.aclose, egress.aclose],
        )

    return factory


async def run_until(
    cfg: AppConfig,
    factory: BackendFactory,
    cond: Callable[[], Awaitable[bool] | bool],
    *,
    timeout: float = 30.0,
) -> int:
    stop = asyncio.Event()
    task = asyncio.create_task(run_app(cfg, backends=factory, stop=stop))
    try:
        async with asyncio.timeout(timeout):
            while not task.done():
                res = cond()
                if (await res) if asyncio.iscoroutine(res) else res:
                    break
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        code = await asyncio.wait_for(task, 30)
    return code


def heartbeat_loops(path: Path) -> dict[str, float]:
    return dict(json.loads(path.read_text(encoding="utf-8"))["loops"])
