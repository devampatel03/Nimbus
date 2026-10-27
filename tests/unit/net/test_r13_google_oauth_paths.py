# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""R13/R6 (P3.4, ADR-0019): `oauth2.googleapis.com` is reachable only for
`POST /token` and `POST /revoke`; the Gmail send/import/insert deny rules keep
applying on every Google host. All denials happen with zero transport calls."""

from __future__ import annotations

import pytest

from src.net import EgressClient, EgressDenied, Service, build_sdk_http_client
from tests.unit.net.net_helpers import RecordingTransport, make_settings


def _client() -> tuple[EgressClient, RecordingTransport]:
    transport = RecordingTransport()
    return EgressClient(make_settings(), transport=transport), transport


@pytest.mark.parametrize("path", ["/token", "/revoke"])
async def test_oauth_token_and_revoke_post_reach_transport(path: str) -> None:
    client, transport = _client()
    async with client:
        resp = await client.request(Service.GOOGLE_OAUTH, "POST", path, content=b"a=b")
    assert resp.status_code == 200
    assert [str(c.url) for c in transport.calls] == [f"https://oauth2.googleapis.com{path}"]


@pytest.mark.parametrize(
    "method,path,reason",
    [
        ("GET", "/token", "google_oauth_method"),
        ("PUT", "/token", "google_oauth_method"),
        ("DELETE", "/revoke", "google_oauth_method"),
        ("POST", "/token/", "google_oauth_path"),
        ("POST", "/tokeninfo", "google_oauth_path"),
        ("POST", "/oauth2/v2/tokeninfo", "google_oauth_path"),
        ("GET", "/oauth2/v3/userinfo", "google_oauth_path"),
        ("POST", "/o/oauth2/v2/auth", "google_oauth_path"),
        ("POST", "/", "google_oauth_path"),
        ("POST", "/gmail/v1/users/me/messages/send", "gmail_send_path"),
        ("POST", "/gmail/v1/users/me/drafts/send", "gmail_send_path"),
    ],
)
async def test_oauth_host_other_paths_and_methods_denied_before_io(
    method: str, path: str, reason: str
) -> None:
    client, transport = _client()
    async with client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(Service.GOOGLE_OAUTH, method, path)
    assert exc.value.reason == reason
    assert transport.calls == []


@pytest.mark.parametrize("service", [Service.GMAIL, Service.GOOGLE_APIS, Service.GOOGLE_OAUTH])
@pytest.mark.parametrize(
    "path",
    [
        "/gmail/v1/users/me/messages/send",
        "/gmail/v1/users/me/messages:send",
        "/upload/gmail/v1/users/me/messages/send",
        "/gmail/v1/users/me/drafts/send",
        "/gmail/v1/users/me/messages/import",
        "/gmail/v1/users/me/messages/insert",
        "/gmail/v1/users/me/%6Dessages/send",
    ],
)
async def test_gmail_send_paths_denied_on_every_google_host(service: Service, path: str) -> None:
    client, transport = _client()
    async with client:
        with pytest.raises(EgressDenied):
            await client.request(service, "POST", path)
    assert transport.calls == []


async def test_gmail_draft_create_and_calendar_paths_still_allowed() -> None:
    client, transport = _client()
    async with client:
        await client.request(Service.GMAIL, "POST", "/gmail/v1/users/me/drafts", json={})
        await client.request(Service.GOOGLE_APIS, "GET", "/calendar/v3/calendars/primary/events")
    assert len(transport.calls) == 2


async def test_sdk_client_pinned_to_oauth_host_enforces_paths_too() -> None:
    transport = RecordingTransport()
    http = build_sdk_http_client(Service.GOOGLE_OAUTH, make_settings(), transport=transport)
    async with http:
        with pytest.raises(EgressDenied):
            await http.get("https://oauth2.googleapis.com/oauth2/v3/userinfo")
        ok = await http.post("https://oauth2.googleapis.com/token", content=b"x=y")
    assert ok.status_code == 200
    assert len(transport.calls) == 1
