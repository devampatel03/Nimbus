# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""One-time installed-app OAuth flow (R3, R15): PKCE, state check, loopback
server (127.0.0.1 only, one request, timeout), end-to-end against a fake
token endpoint."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from src.integrations.google.auth import (
    ALLOWED_SCOPES,
    DEFAULT_SCOPES,
    GoogleAuthError,
    GoogleReauthRequired,
    InMemoryTokenStore,
    LoopbackServer,
    authorize_interactive,
    build_authorization_url,
    generate_pkce,
    pkce_challenge,
)
from tests.unit.integrations.google.auth_helpers import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    SCOPE_CAL,
    SCOPE_SEND,
    FakeTokenEndpoint,
    auth_settings,
    fixture,
    json_response,
    ok,
)

# ---- PKCE (RFC 7636) ------------------------------------------------------


RFC7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # pragma: allowlist secret
RFC7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"  # pragma: allowlist secret


def test_pkce_challenge_matches_rfc7636_appendix_b_vector() -> None:
    assert pkce_challenge(RFC7636_VERIFIER) == RFC7636_CHALLENGE


def test_generated_pkce_pair_is_valid_and_fresh() -> None:
    a, b = generate_pkce(), generate_pkce()
    v = a.verifier.get_secret_value()
    assert 43 <= len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9_\-]+", v)
    digest = hashlib.sha256(v.encode("ascii")).digest()
    assert a.challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert "=" not in a.challenge
    assert a.verifier.get_secret_value() != b.verifier.get_secret_value()
    assert v not in repr(a) and v not in str(a)


def test_authorization_url_carries_pkce_state_and_exact_scopes() -> None:
    url = build_authorization_url(
        client_id=CLIENT_ID,
        redirect_uri="http://127.0.0.1:5555/oauth2callback",
        scopes=DEFAULT_SCOPES,
        state="st4te",
        code_challenge="chal",
    )
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "accounts.google.com",
        "/o/oauth2/v2/auth",
    )
    assert q["response_type"] == "code"
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == "http://127.0.0.1:5555/oauth2callback"
    assert q["state"] == "st4te"
    assert q["code_challenge"] == "chal" and q["code_challenge_method"] == "S256"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert set(q["scope"].split()) == set(ALLOWED_SCOPES)
    assert "client_secret" not in url


def test_authorization_url_refuses_scopes_outside_the_set() -> None:
    with pytest.raises(GoogleReauthRequired):
        build_authorization_url(
            client_id=CLIENT_ID,
            redirect_uri="http://127.0.0.1:1/oauth2callback",
            scopes=[SCOPE_CAL, SCOPE_SEND],
            state="s",
            code_challenge="c",
        )


# ---- loopback server ------------------------------------------------------


async def _http_get(port: int, target: str, *, host: str | None = None, method: str = "GET") -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    host_header = host if host is not None else f"127.0.0.1:{port}"
    writer.write(f"{method} {target} HTTP/1.1\r\nHost: {host_header}\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return data.decode("latin-1")


async def test_loopback_binds_127_0_0_1_only_on_an_ephemeral_port() -> None:
    async with LoopbackServer(timeout_s=5) as srv:
        assert srv.redirect_uri == f"http://127.0.0.1:{srv.port}/oauth2callback"
        assert srv.port > 0
        assert srv.bound_hosts == ["127.0.0.1"]


async def test_loopback_returns_params_then_shuts_down_after_one_request() -> None:
    async with LoopbackServer(timeout_s=5) as srv:
        port = srv.port
        waiter = asyncio.create_task(srv.wait())
        reply = await _http_get(port, f"/oauth2callback?code={AUTH_CODE}&state=abc")
        params = await waiter
        assert params.state == "abc"
        assert params.code is not None and params.code.get_secret_value() == AUTH_CODE
        assert params.error is None
        assert reply.startswith("HTTP/1.1 200")
        assert AUTH_CODE not in reply  # response page never echoes the code
        assert "no-store" in reply.lower()
        with pytest.raises(OSError):  # listener is closed: a second request is refused
            await asyncio.open_connection("127.0.0.1", port)
    assert AUTH_CODE not in repr(params)


async def test_loopback_error_param_is_reported_without_a_code() -> None:
    async with LoopbackServer(timeout_s=5) as srv:
        waiter = asyncio.create_task(srv.wait())
        await _http_get(srv.port, "/oauth2callback?error=access_denied&state=abc")
        params = await waiter
    assert params.error == "access_denied" and params.code is None


async def test_loopback_times_out_with_fixed_code_and_closes() -> None:
    async with LoopbackServer(timeout_s=0.2) as srv:
        port = srv.port
        with pytest.raises(GoogleAuthError) as exc:
            await srv.wait()
        assert exc.value.code == "callback_timeout"
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)


async def test_loopback_rejects_foreign_host_header_dns_rebinding() -> None:
    async with LoopbackServer(timeout_s=5) as srv:
        waiter = asyncio.create_task(srv.wait())
        reply = await _http_get(
            srv.port, f"/oauth2callback?code={AUTH_CODE}&state=abc", host="evil.example.com"
        )
        assert reply.startswith("HTTP/1.1 400")
        with pytest.raises(GoogleAuthError) as exc:
            await waiter
    assert exc.value.code == "bad_callback"


@pytest.mark.parametrize(
    "target,method",
    [("/other?code=x&state=y", "GET"), ("/oauth2callback?code=x&state=y", "POST")],
)
async def test_loopback_wrong_path_or_method_is_bad_callback(target: str, method: str) -> None:
    async with LoopbackServer(timeout_s=5) as srv:
        waiter = asyncio.create_task(srv.wait())
        reply = await _http_get(srv.port, target, method=method)
        assert reply.startswith(("HTTP/1.1 404", "HTTP/1.1 405"))
        with pytest.raises(GoogleAuthError) as exc:
            await waiter
    assert exc.value.code == "bad_callback"


async def test_loopback_idle_connection_does_not_count_as_the_one_request() -> None:
    async with LoopbackServer(timeout_s=5, request_read_timeout_s=0.1) as srv:
        waiter = asyncio.create_task(srv.wait())
        _idle_r, idle_w = await asyncio.open_connection("127.0.0.1", srv.port)  # sends nothing
        await asyncio.sleep(0.3)
        await _http_get(srv.port, "/oauth2callback?code=c0de&state=abc")
        params = await waiter
        idle_w.close()
    assert params.state == "abc"


# ---- end to end -----------------------------------------------------------


class _Browser:
    """Plays the user's browser: follows the printed URL to the loopback."""

    def __init__(self, *, state_override: str | None = None, error: str | None = None) -> None:
        self.url: str | None = None
        self.state_override = state_override
        self.error = error
        self.tasks: list[asyncio.Task[str]] = []

    def __call__(self, url: str) -> None:
        self.url = url
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        redirect = urlsplit(q["redirect_uri"])
        state = self.state_override or q["state"]
        tail = f"error={self.error}" if self.error else f"code={AUTH_CODE}"
        target = f"{redirect.path}?{tail}&state={state}"
        assert redirect.port is not None
        self.tasks.append(asyncio.ensure_future(_http_get(redirect.port, target)))


async def test_authorize_end_to_end_stores_refresh_token_and_scopes() -> None:
    fake = FakeTokenEndpoint(ok("token_exchange_ok.json"))
    store = InMemoryTokenStore()
    browser = _Browser()
    async with fake.egress() as egress:
        granted = await authorize_interactive(
            settings=auth_settings(),
            egress=egress,
            store=store,
            scopes=DEFAULT_SCOPES,
            present_url=browser,
            timeout_s=5,
        )
    assert granted == ALLOWED_SCOPES
    creds = await store.load()
    assert creds is not None
    assert creds.refresh_token.get_secret_value() == REFRESH_TOKEN
    assert creds.scopes == ALLOWED_SCOPES
    # code exchange: PKCE verifier matches the challenge in the URL; secret sent
    (form,) = fake.forms
    assert fake.paths == ["/token"]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == AUTH_CODE
    assert form["client_id"] == CLIENT_ID and form["client_secret"] == CLIENT_SECRET
    assert browser.url is not None
    q = parse_qs(urlsplit(browser.url).query)
    assert pkce_challenge(form["code_verifier"]) == q["code_challenge"][0]
    assert form["redirect_uri"] == q["redirect_uri"][0]
    await asyncio.gather(*browser.tasks)


async def test_state_mismatch_is_rejected_and_nothing_is_exchanged_or_stored() -> None:
    fake = FakeTokenEndpoint()
    store = InMemoryTokenStore()
    async with fake.egress() as egress:
        with pytest.raises(GoogleAuthError) as exc:
            await authorize_interactive(
                settings=auth_settings(),
                egress=egress,
                store=store,
                scopes=DEFAULT_SCOPES,
                present_url=_Browser(state_override="forged-state"),
                timeout_s=5,
            )
    assert exc.value.code == "state_mismatch"
    assert fake.calls == 0
    assert await store.load() is None
    assert AUTH_CODE not in str(exc.value) and "forged-state" not in str(exc.value)


async def test_user_denied_consent_maps_to_fixed_code() -> None:
    fake = FakeTokenEndpoint()
    async with fake.egress() as egress:
        with pytest.raises(GoogleAuthError) as exc:
            await authorize_interactive(
                settings=auth_settings(),
                egress=egress,
                store=InMemoryTokenStore(),
                scopes=DEFAULT_SCOPES,
                present_url=_Browser(error="access_denied"),
                timeout_s=5,
            )
    assert exc.value.code == "authorization_denied"
    assert fake.calls == 0


async def test_flow_times_out_when_no_browser_calls_back() -> None:
    fake = FakeTokenEndpoint()
    async with fake.egress() as egress:
        with pytest.raises(GoogleAuthError) as exc:
            await authorize_interactive(
                settings=auth_settings(),
                egress=egress,
                store=InMemoryTokenStore(),
                scopes=DEFAULT_SCOPES,
                present_url=lambda url: None,
                timeout_s=0.2,
            )
    assert exc.value.code == "callback_timeout"


async def test_token_with_extra_scope_is_rejected_revoked_and_never_stored() -> None:
    granted = f"{SCOPE_CAL} {SCOPE_SEND}"
    fake = FakeTokenEndpoint(ok("token_exchange_ok.json", scope=granted), json_response(200, {}))
    store = InMemoryTokenStore()
    async with fake.egress() as egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await authorize_interactive(
                settings=auth_settings(),
                egress=egress,
                store=store,
                scopes=DEFAULT_SCOPES,
                present_url=_Browser(),
                timeout_s=5,
            )
    assert exc.value.code == "scope_violation"
    assert await store.load() is None
    assert fake.paths == ["/token", "/revoke"]  # best-effort revoke of the bad grant
    assert fake.forms[1]["token"] == REFRESH_TOKEN


async def test_exchange_without_refresh_token_is_an_error() -> None:
    body = {k: v for k, v in fixture("token_exchange_ok.json").items() if k != "refresh_token"}
    fake = FakeTokenEndpoint(json_response(200, body))
    store = InMemoryTokenStore()
    async with fake.egress() as egress:
        with pytest.raises(GoogleAuthError) as exc:
            await authorize_interactive(
                settings=auth_settings(),
                egress=egress,
                store=store,
                scopes=DEFAULT_SCOPES,
                present_url=_Browser(),
                timeout_s=5,
            )
    assert exc.value.code == "no_refresh_token"
    assert await store.load() is None
    assert ACCESS_TOKEN not in str(exc.value)


async def test_missing_client_credentials_fail_before_any_listener_or_request() -> None:
    fake = FakeTokenEndpoint()
    presented: list[str] = []
    async with fake.egress() as egress:
        with pytest.raises(GoogleAuthError) as exc:
            await authorize_interactive(
                settings=auth_settings(client_id=None, client_secret=None),
                egress=egress,
                store=InMemoryTokenStore(),
                scopes=DEFAULT_SCOPES,
                present_url=presented.append,
                timeout_s=5,
            )
    assert exc.value.code == "client_not_configured"
    assert presented == [] and fake.calls == 0
