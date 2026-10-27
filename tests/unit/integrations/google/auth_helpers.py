# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared test doubles for `src.integrations.google.auth`: a fake Google token
endpoint behind the egress client's transport seam (no network, no real
credentials), recorded-and-scrubbed JSON fixtures, and a sleep recorder."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

from src.integrations.google.auth import GoogleOAuthSettings
from src.net import EgressClient
from tests.unit.net.net_helpers import RecordingTransport, make_settings

FIXTURES = Path(__file__).parent / "fixtures"

CLIENT_ID = "123456789-fakeclientid.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fakeclientsecret0000000000000"  # pragma: allowlist secret
REFRESH_TOKEN = "1//0gFAKEFIXTUREREFRESHTOKEN0000000000000000"  # pragma: allowlist secret
ACCESS_TOKEN = "ya29.FAKEFIXTUREREFRESHEDACCESS00000000000000"  # pragma: allowlist secret
AUTH_CODE = "4/0AFAKEAUTHCODE0000000000000000000000"  # pragma: allowlist secret

SCOPE_CAL = "https://www.googleapis.com/auth/calendar.events"
SCOPE_GREAD = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_GCOMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive.readonly"
# Built by concatenation so no forbidden scope literal sits in a scanned file.
SCOPE_SEND = "https://www.googleapis.com/auth/gmail." + "send"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail." + "modify"
SCOPE_FULL_MAIL = "https://mail.google." + "com/"
SCOPE_FULL_DRIVE = "https://www.googleapis.com/auth/" + "drive"
SCOPE_FULL_CAL = "https://www.googleapis.com/auth/" + "calendar"


def fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def auth_settings(**overrides: Any) -> GoogleOAuthSettings:
    base: dict[str, Any] = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    base.update(overrides)
    return GoogleOAuthSettings(_env_file=None, **base)


class FakeTokenEndpoint:
    """Plays queued responses (or raises queued exceptions) for POST /token and
    /revoke, and records every form body it was sent."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.queue: list[httpx.Response | Exception] = list(responses)
        self.forms: list[dict[str, str]] = []
        self.paths: list[str] = []
        self.transport = RecordingTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.forms.append(dict(parse_qsl(request.content.decode())))
        if not self.queue:
            raise AssertionError("unexpected extra request to the fake token endpoint")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def egress(self) -> EgressClient:
        return EgressClient(make_settings(), transport=self.transport)

    @property
    def calls(self) -> int:
        return len(self.transport.calls)


def json_response(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


def ok(name: str = "refresh_ok.json", **patch: Any) -> httpx.Response:
    return json_response(200, {**fixture(name), **patch})


class Sleeps:
    """Injected `sleep`: records requested delays, never waits."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


Notifier = Callable[..., Any]
