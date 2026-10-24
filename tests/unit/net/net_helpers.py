# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared helpers for `src.net` tests. Every client is built over a
`RecordingTransport`, so "zero network requests" is asserted directly as
`transport.calls == []` -- no real socket is ever opened."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.net import NetSettings

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]

LLM_URL = "https://llm.example.com/v1"
HEALTHCHECK = "hc-ping.example.com"
BACKUP = "backup.example.net"


class RecordingTransport(httpx.AsyncBaseTransport):
    """Inner transport double. Records every request that reaches it."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self._handler = handler
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self._handler is None:
            return httpx.Response(200, json={"ok": True})
        result = self._handler(request)
        if isinstance(result, httpx.Response):
            return result
        return await result

    async def aclose(self) -> None:
        self.closed = True


def make_settings(**overrides: Any) -> NetSettings:
    base: dict[str, Any] = {
        "llm_base_url": LLM_URL,
        "healthcheck_host": HEALTHCHECK,
        "backup_host": BACKUP,
    }
    base.update(overrides)
    return NetSettings(_env_file=None, **base)
