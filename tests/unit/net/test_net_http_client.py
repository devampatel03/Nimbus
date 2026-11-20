# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""`src.net.http.EgressClient`: redirects, Gmail send denial (R6), size cap,
timeouts, and INV-7 (no secret values in errors or logs)."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from src.net import (
    EgressClient,
    EgressDenied,
    EgressError,
    EgressRedirectBlocked,
    EgressResponse,
    EgressResponseTooLarge,
    EgressTimeout,
    EgressTransportError,
    NetSettings,
    Service,
    build_sdk_http_client,
)
from tests.unit.net.net_helpers import RecordingTransport, make_settings

# Obviously-fake test secret (not a real token format for any provider).
CANARY = "SEKRIT-TEST-VALUE-0123456789"
TG_PATH = f"/bot123456:{CANARY}/getUpdates"
# The only Notion shapes the egress policy accepts (ADR-0021); used as a neutral GET target.
NP = "/v1/pages/0a1b2c3d-0000-4000-8000-0123456789ab"
NP2 = "/v1/pages/0a1b2c3d-0000-4000-8000-0123456789ac"


def _client(transport: RecordingTransport, **settings: Any) -> EgressClient:
    return EgressClient(make_settings(**settings), transport=transport)


# --------------------------------------------------------------------------
# happy path / request shape
# --------------------------------------------------------------------------


async def test_request_shape_and_response() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "d1"}, headers={"x-r": "1"})

    transport = RecordingTransport(handler)
    async with _client(transport) as client:
        resp = await client.request(
            Service.GMAIL,
            "post",
            "/gmail/v1/users/me/drafts",
            params={"alt": "json"},
            headers={"Authorization": "Bearer t"},
            json={"message": {"raw": "eA"}},
        )
    (req,) = transport.calls
    assert req.method == "POST"
    assert str(req.url) == "https://gmail.googleapis.com/gmail/v1/users/me/drafts?alt=json"
    assert req.headers["authorization"] == "Bearer t"
    assert req.headers["user-agent"] == "nimbus/0.1"
    assert json.loads(req.content) == {"message": {"raw": "eA"}}
    assert resp.status_code == 201
    assert resp.is_success
    assert resp.json() == {"id": "d1"}
    assert resp.text == '{"id":"d1"}'
    assert resp.headers["x-r"] == "1"


async def test_raw_content_body_and_non_success_returned_not_raised() -> None:
    transport = RecordingTransport(lambda r: httpx.Response(404, content=b"nope"))
    async with _client(transport) as client:
        resp = await client.request(
            Service.NOTION, "PATCH", NP.replace("pages", "blocks") + "/children", content=b"abc"
        )
    assert transport.calls[0].content == b"abc"
    assert resp.status_code == 404
    assert not resp.is_success


async def test_json_and_content_together_denied() -> None:
    transport = RecordingTransport()
    async with _client(transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(Service.NOTION, "POST", "/v1/search", json={}, content=b"x")
    assert exc.value.reason == "ambiguous_body"
    assert transport.calls == []


async def test_cookies_do_not_persist_across_requests() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"set-cookie": "sid=abc; Path=/"})

    transport = RecordingTransport(handler)
    async with _client(transport) as client:
        await client.request(Service.NOTION, "GET", NP)
        await client.request(Service.NOTION, "GET", NP)
    assert "cookie" not in transport.calls[1].headers


def test_response_repr_hides_body_and_headers() -> None:
    r = EgressResponse(status_code=200, headers={"set-cookie": CANARY}, content=CANARY.encode())
    assert CANARY not in repr(r)


async def test_close_closes_inner_transport() -> None:
    transport = RecordingTransport()
    client = _client(transport)
    await client.aclose()
    assert transport.closed


async def test_default_transport_constructs_without_network() -> None:
    client = EgressClient(make_settings())
    assert client.allowlist.host_for(Service.TELEGRAM) == "api.telegram.org"
    await client.aclose()
    sdk = build_sdk_http_client(Service.TELEGRAM, NetSettings(_env_file=None))
    await sdk.aclose()


def test_default_settings_loaded_when_none_given() -> None:
    client = EgressClient()
    assert client.allowlist.host_for(Service.NOTION) == "api.notion.com"


# --------------------------------------------------------------------------
# redirects
# --------------------------------------------------------------------------


def _redirect_then_ok(status: int, location: str) -> RecordingTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path in ("/start", NP):
            return httpx.Response(status, headers={"location": location})
        return httpx.Response(200, json={"final": req.url.path})

    return RecordingTransport(handler)


OFF_HOST_REDIRECTS = [
    "https://evil.example.com/steal",
    "//evil.example.com/steal",
    "http://api.notion.com/v1/next",  # downgrade
    "https://api.notion.com:8443/v1/next",
    "https://user:pw@api.notion.com/v1/next",  # pragma: allowlist secret (fake)
    "https://api.telegram.org/x",  # allowlisted, but a different host
    "https://api.notion.com.evil.example.com/x",
]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", OFF_HOST_REDIRECTS)
async def test_redirect_to_other_host_blocked(status: int, location: str) -> None:
    transport = _redirect_then_ok(status, location)
    async with _client(transport) as client:
        with pytest.raises(EgressRedirectBlocked):
            await client.request(Service.NOTION, "GET", NP)
    # Only the original request reached the transport; nothing to the target.
    assert [str(r.url) for r in transport.calls] == [f"https://api.notion.com{NP}"]


@pytest.mark.parametrize(
    ("status", "method", "expected_method", "body_kept"),
    [
        (301, "GET", "GET", True),
        (302, "POST", "GET", False),
        (303, "POST", "GET", False),
        (303, "HEAD", "HEAD", False),
        (307, "POST", "POST", True),
        (308, "PUT", "PUT", True),
    ],
)
async def test_same_host_redirect_followed_and_revalidated(
    status: int, method: str, expected_method: str, body_kept: bool
) -> None:
    transport = _redirect_then_ok(status, "/v1/next?page=2")
    async with _client(transport) as client:
        resp = await client.request(
            Service.TELEGRAM, method, "/start", params={"a": "1"}, content=b"payload"
        )
    assert resp.status_code == 200
    _first, second = transport.calls
    assert str(second.url) == "https://api.telegram.org/v1/next?page=2"
    assert second.method == expected_method
    assert (second.content == b"payload") is body_kept


async def test_unparsable_redirect_location_fails_closed() -> None:
    # httpx itself rejects the Location while building its redirect hint;
    # either way the result is a sanitised error and no second request.
    transport = _redirect_then_ok(302, "https://[::1")
    async with _client(transport) as client:
        with pytest.raises(EgressError):
            await client.request(Service.NOTION, "GET", NP)
    assert len(transport.calls) == 1


async def test_same_host_redirect_into_denied_path_blocked() -> None:
    transport = _redirect_then_ok(307, "/gmail/v1/users/me/messages/send")
    async with _client(transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(Service.GMAIL, "POST", "/start")
    assert exc.value.reason == "gmail_send_path"
    assert len(transport.calls) == 1


async def test_same_host_redirect_with_traversal_blocked() -> None:
    transport = _redirect_then_ok(302, "/a/%2e%2e/b")
    async with _client(transport) as client:
        with pytest.raises(EgressDenied):
            await client.request(Service.NOTION, "GET", NP)
    assert len(transport.calls) == 1


async def test_redirect_loop_capped() -> None:
    transport = RecordingTransport(lambda r: httpx.Response(302, headers={"location": NP}))
    async with _client(transport, max_redirects=2) as client:
        with pytest.raises(EgressRedirectBlocked) as exc:
            await client.request(Service.NOTION, "GET", NP)
    assert exc.value.reason == "too_many_redirects"
    assert len(transport.calls) == 3


async def test_zero_redirects_allowed_when_configured() -> None:
    transport = _redirect_then_ok(302, NP2)
    async with _client(transport, max_redirects=0) as client:
        with pytest.raises(EgressRedirectBlocked):
            await client.request(Service.NOTION, "GET", NP)
    assert len(transport.calls) == 1


async def test_3xx_without_location_returned_as_is() -> None:
    transport = RecordingTransport(lambda r: httpx.Response(304))
    async with _client(transport) as client:
        resp = await client.request(Service.NOTION, "GET", NP)
    assert resp.status_code == 304


# --------------------------------------------------------------------------
# R6: Gmail send endpoints denied at the network layer, for any caller
# --------------------------------------------------------------------------

GMAIL_SEND_PATHS = [
    "/gmail/v1/users/me/messages/send",
    "/gmail/v1/users/me/drafts/send",
    "/gmail/v1/users/me/messages/import",
    "/gmail/v1/users/me/messages/insert",
    "/upload/gmail/v1/users/me/messages/send",
    "/upload/gmail/v1/users/me/drafts/send",
    "/gmail/v1/users/someone%40example.com/messages/send",
    "/GMAIL/V1/USERS/ME/MESSAGES/SEND",
    "/gmail/v1/users/me/messages/send/",
    "/gmail/v1/users/me/messages%2Fsend",
    "/gmail/v1/users/me/messages%252Fsend",
    "/gmail/v1/users/me/%6d%65ssages/send",
    "/gmail/v1/users/me/drafts:send",
    "/gmail/v2/users/me/messages/send",
    "/gmail/v1/users/me/messages/send.json",
    "/gmail/v1/users/me/messages/send:x",
]


@pytest.mark.parametrize("service", [Service.GMAIL, Service.GOOGLE_APIS])
@pytest.mark.parametrize("method", ["POST", "PUT", "GET"])
@pytest.mark.parametrize("path", GMAIL_SEND_PATHS)
async def test_gmail_send_paths_denied_with_zero_transport_calls(
    service: Service, method: str, path: str
) -> None:
    transport = RecordingTransport()
    async with _client(transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(service, method, path, json={"raw": "eA"})
    assert exc.value.reason == "gmail_send_path"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("service", "method", "path", "reason"),
    [
        (Service.GMAIL, "POST", "/batch/gmail/v1", "google_batch"),
        (Service.GOOGLE_APIS, "POST", "/batch", "google_batch"),
        (
            Service.GMAIL,
            "POST",
            "/gmail/v1/users/me/settings/forwardingAddresses",
            "gmail_settings_write",
        ),
        (
            Service.GMAIL,
            "PUT",
            "/gmail/v1/users/me/settings/autoForwarding",
            "gmail_settings_write",
        ),
        (
            Service.GOOGLE_APIS,
            "POST",
            "/gmail/v1/users/me/settings/filters",
            "gmail_settings_write",
        ),
    ],
)
async def test_send_equivalent_google_paths_denied(
    service: Service, method: str, path: str, reason: str
) -> None:
    transport = RecordingTransport()
    async with _client(transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(service, method, path)
    assert exc.value.reason == reason
    assert transport.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/gmail/v1/users/me/drafts"),  # drafts.create is allowed (ADR-0007)
        ("PUT", "/gmail/v1/users/me/drafts/d1"),
        ("GET", "/gmail/v1/users/me/messages"),
        ("GET", "/gmail/v1/users/me/messages/m1"),
        ("GET", "/gmail/v1/users/me/settings/filters"),
    ],
)
async def test_gmail_non_send_paths_allowed(method: str, path: str) -> None:
    transport = RecordingTransport()
    async with _client(transport) as client:
        await client.request(Service.GMAIL, method, path)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("service", [s for s in Service if s is not Service.GMAIL])
async def test_send_path_denied_on_every_service(service: Service) -> None:
    # Host-agnostic: any Google-fronted host (incl. an LLM endpoint on
    # *.googleapis.com) could route a Gmail path at the shared front end.
    transport = RecordingTransport()
    async with _client(transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(service, "POST", "/gmail/v1/users/me/messages/send")
    assert exc.value.reason == "gmail_send_path"
    assert transport.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/gmail/v1/users/me/messages/send;x",
        "/gmail/v1/users/me/messages/send%3bx",
        "/gmail/v1/users/me/messages/./send",
        "/gmail/v1/users/me/messages/x/../send",
        "/gmail/v1/users/me//messages/send",
    ],
)
async def test_send_path_obfuscation_rejected(path: str) -> None:
    transport = RecordingTransport()
    async with _client(transport) as client:
        with pytest.raises(EgressDenied):
            await client.request(Service.GMAIL, "POST", path)
    assert transport.calls == []


async def test_gmail_send_denied_on_sdk_client_too() -> None:
    transport = RecordingTransport()
    async with build_sdk_http_client(Service.GMAIL, make_settings(), transport=transport) as c:
        with pytest.raises(EgressDenied) as exc:
            await c.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send")
    assert exc.value.reason == "gmail_send_path"
    assert transport.calls == []


async def test_extra_deny_rule_hook_adds_to_defaults() -> None:
    def deny_notion_delete(service: Service, method: str, path: str) -> str | None:
        return "notion_delete" if service is Service.TELEGRAM and method == "DELETE" else None

    transport = RecordingTransport()
    client = EgressClient(
        make_settings(), transport=transport, extra_deny_rules=[deny_notion_delete]
    )
    async with client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(Service.TELEGRAM, "DELETE", "/v1/blocks/x")
        assert exc.value.reason == "notion_delete"
        # defaults still apply alongside the extra rule
        with pytest.raises(EgressDenied):
            await client.request(Service.GMAIL, "POST", "/gmail/v1/users/me/drafts/send")
    assert transport.calls == []


# --------------------------------------------------------------------------
# response size cap and timeouts
# --------------------------------------------------------------------------


async def test_declared_oversize_response_rejected() -> None:
    transport = RecordingTransport(
        lambda r: httpx.Response(200, headers={"content-length": "999999999"}, content=b"x")
    )
    async with _client(transport) as client:
        with pytest.raises(EgressResponseTooLarge):
            await client.request(Service.NOTION, "GET", NP)


async def test_streamed_oversize_response_rejected() -> None:
    async def body() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"a" * 100

    transport = RecordingTransport(lambda r: httpx.Response(200, content=body()))
    async with _client(transport) as client:
        with pytest.raises(EgressResponseTooLarge):
            await client.request(Service.NOTION, "GET", NP, max_bytes=500)


async def test_decompressed_size_is_capped() -> None:
    bomb = gzip.compress(b"\0" * 200_000)
    transport = RecordingTransport(
        lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, content=bomb)
    )
    async with _client(transport, max_response_bytes=10_000) as client:
        with pytest.raises(EgressResponseTooLarge):
            await client.request(Service.NOTION, "GET", NP)
    assert len(bomb) < 10_000


async def test_response_exactly_at_cap_allowed() -> None:
    transport = RecordingTransport(lambda r: httpx.Response(200, content=b"a" * 64))
    async with _client(transport) as client:
        resp = await client.request(Service.NOTION, "GET", NP, max_bytes=64)
    assert len(resp.content) == 64


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"max_bytes": 0}, "bad_max_bytes"),
        ({"max_bytes": 26 * 1024 * 1024}, "bad_max_bytes"),
        ({"timeout_s": 0}, "bad_timeout"),
        ({"timeout_s": 301}, "bad_timeout"),
    ],
)
async def test_out_of_range_limits_denied(kwargs: dict[str, Any], reason: str) -> None:
    transport = RecordingTransport()
    async with _client(transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(Service.NOTION, "GET", NP, **kwargs)
    assert exc.value.reason == reason
    assert transport.calls == []


async def test_overall_deadline_enforced() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    transport = RecordingTransport(slow)
    async with _client(transport) as client:
        with pytest.raises(EgressTimeout):
            await client.request(Service.NOTION, "GET", NP, timeout_s=0.05)


def test_timeouts_configured_from_settings() -> None:
    client = EgressClient(
        make_settings(connect_timeout_s=1.5, read_timeout_s=2.5), transport=RecordingTransport()
    )
    t = client._client.timeout
    assert (t.connect, t.read) == (1.5, 2.5)
    assert client._client.follow_redirects is False
    assert client._client.trust_env is False


# --------------------------------------------------------------------------
# INV-7: no secret values in errors or logs
# --------------------------------------------------------------------------


def _raise(exc: BaseException) -> Any:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _assert_no_secret(exc: EgressError, caplog: pytest.LogCaptureFixture) -> None:
    assert CANARY not in str(exc)
    assert CANARY not in repr(exc)
    assert CANARY not in repr(exc.args)
    assert exc.__cause__ is None
    assert exc.__suppress_context__ or exc.__context__ is None
    for record in caplog.records:
        assert CANARY not in record.getMessage()
        assert CANARY not in repr(record.args)
    assert CANARY not in caplog.text


FAILURE_CASES = [
    pytest.param(
        _raise(httpx.ConnectError(f"cannot reach https://api.telegram.org{TG_PATH}")),
        EgressTransportError,
        id="connect_error_with_url",
    ),
    pytest.param(
        _raise(httpx.ReadTimeout(f"timeout on {TG_PATH}")), EgressTimeout, id="read_timeout"
    ),
    pytest.param(
        _raise(RuntimeError(f"boom token={CANARY}")), EgressTransportError, id="unexpected_error"
    ),
    pytest.param(
        lambda r: httpx.Response(200, content=b"x" * 2048),
        EgressResponseTooLarge,
        id="too_large",
    ),
    pytest.param(
        lambda r: httpx.Response(302, headers={"location": f"https://evil.example.com/{CANARY}"}),
        EgressRedirectBlocked,
        id="redirect_blocked",
    ),
]


@pytest.mark.parametrize(("handler", "expected"), FAILURE_CASES)
@pytest.mark.parametrize(
    "where",
    ["path", "header", "query", "body"],
)
async def test_failures_never_expose_secret(
    handler: Any, expected: type[EgressError], where: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    kwargs: dict[str, Any] = {"max_bytes": 1024}
    path = "/bot123456:public/getUpdates"
    if where == "path":
        path = TG_PATH
    elif where == "header":
        kwargs["headers"] = {"Authorization": f"Bearer {CANARY}"}
    elif where == "query":
        kwargs["params"] = {"token": CANARY}
    else:
        kwargs["json"] = {"token": CANARY}
    transport = RecordingTransport(handler)
    async with _client(transport) as client:
        with pytest.raises(expected) as exc:
            await client.request(Service.TELEGRAM, "POST", path, **kwargs)
    _assert_no_secret(exc.value, caplog)


@pytest.mark.parametrize(
    "path",
    [f"/gmail/v1/users/me/messages/send?k={CANARY}", f"/gmail/v1/users/{CANARY}/messages/send"],
)
async def test_denials_never_expose_secret(path: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    transport = RecordingTransport()
    async with _client(transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(
                Service.GMAIL, "POST", path, headers={"Authorization": f"Bearer {CANARY}"}
            )
    _assert_no_secret(exc.value, caplog)
    assert transport.calls == []


async def test_successful_request_logs_never_contain_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # httpx itself logs "HTTP Request: POST <full url>" at INFO; it must be silenced.
    caplog.set_level(logging.DEBUG)
    transport = RecordingTransport()
    async with _client(transport) as client:
        await client.request(
            Service.TELEGRAM, "POST", TG_PATH, headers={"Authorization": f"Bearer {CANARY}"}
        )
    assert transport.calls  # the request really went through httpx's client
    assert CANARY not in caplog.text
    assert any("egress ok service=telegram" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]


def test_settings_validation_error_hides_input() -> None:
    with pytest.raises(Exception) as exc:
        make_settings(llm_base_url=f"http://user:{CANARY}@llm.example.com/v1")
    assert CANARY not in str(exc.value)
    assert CANARY not in repr(exc.value)
