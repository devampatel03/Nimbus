# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""R13: outbound egress is restricted to an allowlist (TARGET_DESIGN section 3).

- `Service` resolves to exactly the allowlisted host set.
- Any other host / non-Service target raises, with ZERO transport calls.
- The client API has no URL/host parameter (no generic fetch surface).
"""

from __future__ import annotations

import inspect
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from src.net import (
    FIXED_HOSTS,
    Allowlist,
    EgressClient,
    EgressDenied,
    NetSettings,
    Service,
    build_sdk_http_client,
)
from tests.unit.net.net_helpers import BACKUP, HEALTHCHECK, RecordingTransport, make_settings

EXPECTED_HOSTS = {
    Service.TELEGRAM: "api.telegram.org",
    Service.LLM: "llm.example.com",
    Service.GOOGLE_OAUTH: "oauth2.googleapis.com",
    Service.GOOGLE_APIS: "www.googleapis.com",
    Service.GMAIL: "gmail.googleapis.com",
    Service.NOTION: "api.notion.com",
    Service.HEALTHCHECK: HEALTHCHECK,
    Service.BACKUP: BACKUP,
}


def test_service_maps_to_exactly_the_allowlisted_host_set() -> None:
    allow = Allowlist.from_settings(make_settings())
    assert dict(allow.hosts) == EXPECTED_HOSTS
    assert set(Service) == set(EXPECTED_HOSTS)


def test_default_settings_enable_only_fixed_vendor_hosts() -> None:
    allow = Allowlist.from_settings(NetSettings(_env_file=None))
    assert dict(allow.hosts) == dict(FIXED_HOSTS)


def test_llm_host_read_from_shared_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://api.provider.example.org/v1")
    assert Allowlist.from_settings(NetSettings(_env_file=None)).host_for(Service.LLM) == (
        "api.provider.example.org"
    )


def test_enabled_services_can_only_narrow() -> None:
    allow = Allowlist.from_settings(
        make_settings(enabled_services=frozenset({"telegram", "not-a-service"}))
    )
    assert dict(allow.hosts) == {Service.TELEGRAM: "api.telegram.org"}


# Non-Service targets: hosts, URLs, look-alike strings, wrong types.
NON_SERVICE_TARGETS: list[Any] = [
    "evil.example.com",
    "https://evil.example.com/x",
    "api.telegram.org",
    "telegram",  # the enum's *value*, but not a Service instance
    None,
    42,
    httpx.URL("https://evil.example.com"),
]


@pytest.mark.parametrize("target", NON_SERVICE_TARGETS, ids=repr)
async def test_non_service_target_raises_with_zero_transport_calls(target: Any) -> None:
    transport = RecordingTransport()
    async with EgressClient(make_settings(), transport=transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(target, "GET", "/")
    assert exc.value.reason == "unknown_service"
    assert transport.calls == []


@pytest.mark.parametrize("service", [Service.LLM, Service.HEALTHCHECK, Service.BACKUP])
async def test_unconfigured_service_is_default_deny(service: Service) -> None:
    transport = RecordingTransport()
    async with EgressClient(NetSettings(_env_file=None), transport=transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(service, "GET", "/")
    assert exc.value.reason == "service_disabled"
    assert transport.calls == []


async def test_disabled_service_is_denied() -> None:
    transport = RecordingTransport()
    settings = make_settings(enabled_services=frozenset({"telegram"}))
    async with EgressClient(settings, transport=transport) as client:
        with pytest.raises(EgressDenied):
            await client.request(Service.NOTION, "GET", "/v1/users/me")
    assert transport.calls == []


# Paths that try to change the effective host, smuggle a query, or traverse.
HOST_SMUGGLING_PATHS = [
    "",
    "evil.example.com/x",
    "@evil.example.com/x",
    "//evil.example.com/x",
    "/%2F/evil.example.com",
    "/a/../b",
    "/a/./b",
    "/%2e%2e/x",
    "/%252e%252e/x",
    "/%25252525252e",
    "/x?url=https://evil.example.com",
    "/x%3Furl=evil",
    "/x#frag",
    "/x\\evil.example.com",
    "/x\r\nHost: evil.example.com",
    "/x y",
    "/x\x00",
    "/" + "a" * 2048,
]


@pytest.mark.parametrize("path", HOST_SMUGGLING_PATHS, ids=repr)
async def test_bad_path_raises_with_zero_transport_calls(path: str) -> None:
    transport = RecordingTransport()
    async with EgressClient(make_settings(), transport=transport) as client:
        with pytest.raises(EgressDenied):
            await client.request(Service.TELEGRAM, "GET", path)
    assert transport.calls == []


async def test_non_string_path_denied() -> None:
    transport = RecordingTransport()
    async with EgressClient(make_settings(), transport=transport) as client:
        with pytest.raises(EgressDenied):
            await client.request(Service.TELEGRAM, "GET", b"/x")  # type: ignore[arg-type]
    assert transport.calls == []


@pytest.mark.parametrize(
    "header",
    [
        "Host",
        "host",
        "X-Forwarded-Host",
        "Forwarded",
        "X-HTTP-Method-Override",
        "Proxy-Authorization",
        "Proxy-Anything",
        "Transfer-Encoding",
    ],
)
async def test_routing_headers_denied_with_zero_transport_calls(header: str) -> None:
    transport = RecordingTransport()
    async with EgressClient(make_settings(), transport=transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(
                Service.GOOGLE_APIS,
                "GET",
                "/calendar/v3/calendars/primary/events",
                headers={header: "evil.example.com"},
            )
    assert exc.value.reason == "forbidden_header"
    assert transport.calls == []


@pytest.mark.parametrize(
    "headers", [{"X-A": "v\r\nHost: evil"}, {"X-A": "v\n"}, {"X A": "v"}], ids=repr
)
async def test_header_injection_denied(headers: dict[str, str]) -> None:
    transport = RecordingTransport()
    async with EgressClient(make_settings(), transport=transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(Service.NOTION, "POST", "/v1/search", headers=headers)
    assert exc.value.reason == "bad_header_value"
    assert transport.calls == []


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "OPTIONS", "GET ", "", None])
async def test_unlisted_method_denied(method: Any) -> None:
    transport = RecordingTransport()
    async with EgressClient(make_settings(), transport=transport) as client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(Service.NOTION, method, "/v1/users/me")
    assert exc.value.reason == "method_not_allowed"
    assert transport.calls == []


@pytest.mark.parametrize("service", list(EXPECTED_HOSTS))
async def test_allowlisted_service_reaches_its_host_over_https(service: Service) -> None:
    transport = RecordingTransport()
    async with EgressClient(make_settings(), transport=transport) as client:
        # oauth2.googleapis.com only serves POST /token and /revoke (ADR-0019).
        if service is Service.GOOGLE_OAUTH:
            resp = await client.request(service, "POST", "/token")
        elif service is Service.NOTION:  # only four Notion calls exist (ADR-0021)
            resp = await client.request(service, "POST", "/v1/search")
        else:
            resp = await client.request(service, "GET", "/ok")
    assert resp.status_code == 200
    (req,) = transport.calls
    assert req.url.scheme == "https"
    assert req.url.host == EXPECTED_HOSTS[service]
    assert req.url.port is None


# ---- settings validation: env cannot widen egress to arbitrary targets ----

BAD_LLM_URLS = [
    "http://llm.example.com/v1",
    "https://user:pass@llm.example.com/v1",  # pragma: allowlist secret (fake)
    "https://llm.example.com:8443/v1",
    "https://llm.example.com:notaport/v1",
    "https://localhost/v1",
    "https://127.0.0.1/v1",
    "https://169.254.169.254/latest",
    "https://[::1]/v1",
    "ftp://llm.example.com",
]


@pytest.mark.parametrize("url", BAD_LLM_URLS)
def test_bad_llm_base_url_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(llm_base_url=url)


@pytest.mark.parametrize("host", ["localhost", "10.0.0.1", "evil", "a..b.com", "-x.com"])
def test_bad_configurable_host_rejected(host: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(backup_host=host)


def test_blank_optional_hosts_mean_disabled() -> None:
    s = make_settings(llm_base_url="  ", healthcheck_host="", backup_host=" ")
    assert set(Allowlist.from_settings(s).hosts) == set(FIXED_HOSTS)


# ---- no generic web-fetch surface (R13) ----


def test_client_api_has_no_url_or_host_parameter() -> None:
    params = list(inspect.signature(EgressClient.request).parameters)
    assert not set(params) & {"url", "host", "base_url", "netloc"}
    assert params[:4] == ["self", "service", "method", "path"]


# ---- SDK client (openai http_client=) is pinned to one host ----


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/v1/chat/completions",
        "https://api.telegram.org/x",  # allowlisted elsewhere, but not for this client
        "http://llm.example.com/v1/chat/completions",
        "https://llm.example.com:8443/v1/chat/completions",
        "https://u:p@llm.example.com/v1/chat/completions",  # pragma: allowlist secret
    ],
)
async def test_sdk_client_off_host_raises_with_zero_transport_calls(url: str) -> None:
    transport = RecordingTransport()
    async with build_sdk_http_client(Service.LLM, make_settings(), transport=transport) as c:
        with pytest.raises(EgressDenied):
            await c.post(url, json={})
    assert transport.calls == []


async def test_sdk_client_host_header_override_denied() -> None:
    transport = RecordingTransport()
    async with build_sdk_http_client(Service.LLM, make_settings(), transport=transport) as c:
        with pytest.raises(EgressDenied) as exc:
            await c.get("https://llm.example.com/v1/models", headers={"Host": "evil.example.com"})
    assert exc.value.reason == "host_header_mismatch"
    assert transport.calls == []


async def test_sdk_client_does_not_follow_redirects() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/"})

    transport = RecordingTransport(handler)
    async with build_sdk_http_client(Service.LLM, make_settings(), transport=transport) as c:
        resp = await c.get("https://llm.example.com/v1/models")
    assert resp.status_code == 302
    assert len(transport.calls) == 1


async def test_sdk_client_allows_its_pinned_host() -> None:
    transport = RecordingTransport()
    async with build_sdk_http_client(Service.LLM, make_settings(), transport=transport) as c:
        resp = await c.post("https://llm.example.com/v1/chat/completions", json={})
    assert resp.status_code == 200
    assert len(transport.calls) == 1


def test_sdk_client_for_unconfigured_service_raises() -> None:
    with pytest.raises(EgressDenied):
        build_sdk_http_client(Service.LLM, NetSettings(_env_file=None))
