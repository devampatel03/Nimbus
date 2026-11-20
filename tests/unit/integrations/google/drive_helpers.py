# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Test doubles for Drive (P3.8): a fake Drive API behind the egress client's
transport seam (recorded, scrubbed fixtures; no network, no real credentials)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import SecretStr

from src.integrations.google.drive_api import DriveReader
from src.net import EgressClient
from tests.unit.net.net_helpers import RecordingTransport, make_settings

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "ya29.FAKEDRIVEACCESSTOKEN000000000000000"  # pragma: allowlist secret

ID_DOC = "1FakeDocFakeDocFakeDoc00000001"
ID_PDF = "1FakePdfFakePdfFakePdf00000002"
ID_BIG = "1BigBigBigBigBigBigBig0000000001"
ID_INJECT = "1InjectInjectInjectInject00000001"


def fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeAuth:
    def __init__(self) -> None:
        self.required: list[list[str]] = []

    async def access_token(self, required_scopes: object = ()) -> SecretStr:
        self.required.append(list(required_scopes))  # type: ignore[call-overload]
        return SecretStr(TOKEN)


class FakeDrive:
    """Routes by (path, alt/export) to queued fixture bytes."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes = routes
        self.transport = RecordingTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path
        if "export" not in key and request.url.params.get("alt") == "media":
            key += "?media"
        status, body = self.routes.get(key, (404, b"{}"))
        return httpx.Response(status, content=body)

    @property
    def requests(self) -> list[httpx.Request]:
        return self.transport.calls

    def reader(self, auth: FakeAuth | None = None) -> DriveReader:
        return DriveReader(
            EgressClient(make_settings(), transport=self.transport), auth or FakeAuth()
        )


Handler = Callable[[httpx.Request], httpx.Response]
