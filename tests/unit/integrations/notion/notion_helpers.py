# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Test doubles for Notion (P3.7): a fake API behind the egress client's transport
seam. Fixtures are recorded and scrubbed (fake ids, fake token); no network."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from src.integrations.notion.api import NotionClient
from src.net import EgressClient
from tests.unit.net.net_helpers import RecordingTransport, make_settings

FIXTURES = Path(__file__).parents[2] / "tools" / "fixtures"
TOKEN = "ntn_FAKEFIXTURENOTIONTOKEN0000000000000000"  # pragma: allowlist secret
P1 = "11111111-1111-4111-8111-111111111111"
P2 = "22222222-2222-4222-8222-222222222222"
P3 = "33333333-3333-4333-8333-333333333333"


def fx(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


Reply = httpx.Response | Callable[[httpx.Request], httpx.Response]


class FakeNotion:
    """Routes `(METHOD, path)` to a queue of replies (the last one repeats)."""

    def __init__(self, routes: dict[tuple[str, str], list[Reply]] | None = None) -> None:
        self.routes = routes or {}
        self.sleeps: list[float] = []
        self.transport = RecordingTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(404, json=fx("notion_error_404.json"))
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return reply(request) if callable(reply) else reply

    @property
    def requests(self) -> list[httpx.Request]:
        return self.transport.calls

    def methods(self) -> list[str]:
        return [r.method for r in self.requests]

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def client(self) -> NotionClient:
        egress = EgressClient(make_settings(), transport=self.transport)
        return NotionClient(egress, SecretStr(TOKEN), sleep=self._sleep)


def ok(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)
