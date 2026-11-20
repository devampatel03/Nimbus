# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Refresh / access-token exchange against a fake token endpoint (no network):
caching, scope-lint on what the token reports, fixed-code failure modes,
bounded backoff, fail-fast after invalid_grant, re-auth notification, revoke,
and INV-7 (no token, code, verifier or client secret in errors or logs)."""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from src.core.logging import install_log_scrubbing
from src.integrations.google.auth import (
    BASE_SCOPES,
    GoogleAuth,
    GoogleAuthError,
    GoogleReauthRequired,
    GoogleTokenClient,
    InMemoryTokenStore,
    ReauthNotice,
    StoredCredentials,
)
from src.net import NetSettings
from src.net.http import EgressClient
from tests.unit.integrations.google.auth_helpers import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    SCOPE_CAL,
    SCOPE_DRIVE,
    SCOPE_FULL_MAIL,
    SCOPE_GCOMPOSE,
    SCOPE_GREAD,
    SCOPE_SEND,
    Clock,
    FakeTokenEndpoint,
    Sleeps,
    auth_settings,
    fixture,
    json_response,
    ok,
)
from tests.unit.net.net_helpers import RecordingTransport

CANARIES = (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, AUTH_CODE)
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def creds(refresh: str = REFRESH_TOKEN, scopes: frozenset[str] = BASE_SCOPES) -> StoredCredentials:
    return StoredCredentials(refresh_token=SecretStr(refresh), scopes=scopes, obtained_at=NOW)


class Harness:
    def __init__(
        self,
        *responses: httpx.Response | Exception,
        stored: StoredCredentials | None = None,
        max_retries: int = 2,
        allow_compose: bool = False,
    ) -> None:
        self.fake = FakeTokenEndpoint(*responses)
        self.store = InMemoryTokenStore(stored, allow_compose=allow_compose)
        self.sleeps = Sleeps()
        self.clock = Clock()
        self.notices: list[ReauthNotice] = []
        self.egress: EgressClient = self.fake.egress()
        self.client = GoogleTokenClient(
            auth_settings(max_retries=max_retries, allow_gmail_compose=allow_compose),
            self.egress,
            sleep=self.sleeps,
            jitter=lambda: 1.0,
        )
        self.auth = GoogleAuth(self.client, self.store, notifier=self._notify, clock=self.clock)

    async def _notify(self, notice: ReauthNotice) -> None:
        self.notices.append(notice)

    async def aclose(self) -> None:
        await self.egress.aclose()


# ---- happy path / caching -------------------------------------------------


async def test_refresh_returns_access_token_and_caches_until_expiry() -> None:
    h = Harness(ok(), ok(access_token=ACCESS_TOKEN + "2"), stored=creds())
    async with h.egress:
        t1 = await h.auth.access_token()
        t2 = await h.auth.access_token()
        assert t1.get_secret_value() == ACCESS_TOKEN == t2.get_secret_value()
        assert h.fake.calls == 1
        (form,) = h.fake.forms
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == REFRESH_TOKEN
        assert h.fake.paths == ["/token"]
        h.clock.now += 3599 - 30  # inside the 60 s safety margin -> refresh again
        t3 = await h.auth.access_token()
    assert t3.get_secret_value() == ACCESS_TOKEN + "2"
    assert h.fake.calls == 2
    assert ACCESS_TOKEN not in repr(t1)  # SecretStr masks


async def test_concurrent_callers_share_one_refresh() -> None:
    h = Harness(ok(), stored=creds())
    async with h.egress:
        results = await asyncio.gather(*(h.auth.access_token() for _ in range(8)))
    assert h.fake.calls == 1
    assert {r.get_secret_value() for r in results} == {ACCESS_TOKEN}


async def test_rotated_refresh_token_in_response_is_persisted() -> None:
    h = Harness(ok(refresh_token="1//0gROTATEDREFRESHTOKEN00000000000000"), stored=creds())
    async with h.egress:
        await h.auth.access_token()
    loaded = await h.store.load()
    assert loaded is not None
    assert loaded.refresh_token.get_secret_value() == "1//0gROTATEDREFRESHTOKEN00000000000000"


async def test_required_scopes_subset_is_enforced() -> None:
    h = Harness(ok(scope=SCOPE_CAL), stored=creds())
    async with h.egress:
        assert (await h.auth.access_token(required_scopes=[SCOPE_CAL])).get_secret_value()
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token(required_scopes=[SCOPE_GCOMPOSE])
    assert exc.value.code == "scope_missing"


# ---- scope-lint on what the token reports ---------------------------------


@pytest.mark.parametrize("extra", [SCOPE_SEND, SCOPE_FULL_MAIL])
async def test_refresh_response_with_extra_scope_is_rejected_and_unused(extra: str) -> None:
    h = Harness(ok(scope=f"{SCOPE_CAL} {extra}"), stored=creds())
    async with h.egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
        assert exc.value.code == "scope_violation"
        # the offending token is neither cached nor returned; further calls fail fast
        with pytest.raises(GoogleReauthRequired):
            await h.auth.access_token()
    assert h.fake.calls == 1
    assert len(h.notices) == 1 and h.notices[0].code == "scope_violation"


async def test_stored_token_reporting_extra_scopes_is_rejected_without_network() -> None:
    h = Harness(stored=creds(scopes=frozenset({SCOPE_CAL, SCOPE_SEND})))
    async with h.egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
    assert exc.value.code == "scope_violation"
    assert h.fake.calls == 0


# ---- scope-lint: H16 compose gate + refresh may never widen ---------------

WITH_COMPOSE = frozenset({SCOPE_CAL, SCOPE_GREAD, SCOPE_GCOMPOSE, SCOPE_DRIVE})


async def test_refresh_may_not_widen_beyond_stored_scopes() -> None:
    """Stored {calendar}; Google reports calendar + gmail.readonly (allowed
    in general, but never granted for this token) -> rejected, token dead."""
    h = Harness(ok(scope=f"{SCOPE_CAL} {SCOPE_GREAD}"), stored=creds(scopes=frozenset({SCOPE_CAL})))
    async with h.egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
        assert exc.value.code == "scope_violation"
        with pytest.raises(GoogleReauthRequired):
            await h.auth.access_token()  # marked dead: no second request
    assert h.fake.calls == 1


async def test_refresh_returning_compose_over_stored_calendar_is_rejected() -> None:
    h = Harness(
        ok(scope=f"{SCOPE_CAL} {SCOPE_GCOMPOSE}"),
        stored=creds(scopes=frozenset({SCOPE_CAL})),
        allow_compose=True,  # even with the flag on, a refresh cannot add scopes
    )
    async with h.egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
    assert exc.value.code == "scope_violation"


async def test_refresh_without_scope_field_keeps_stored_scopes() -> None:
    body = {k: v for k, v in fixture("refresh_ok.json").items() if k != "scope"}
    h = Harness(json_response(200, body), stored=creds(scopes=frozenset({SCOPE_CAL})))
    async with h.egress:
        assert (await h.auth.access_token(required_scopes=[SCOPE_CAL])).get_secret_value()
        # cached scopes are exactly the stored ones, not the wider allowed set
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token(required_scopes=[SCOPE_GREAD])
    assert exc.value.code == "scope_missing"


async def test_stored_compose_is_rejected_when_flag_off_without_network() -> None:
    h = Harness(stored=creds(scopes=WITH_COMPOSE))
    async with h.egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
    assert exc.value.code == "scope_violation" and h.fake.calls == 0


async def test_compose_in_refresh_is_rejected_when_flag_off_and_accepted_when_on() -> None:
    body = ok(scope=" ".join(sorted(WITH_COMPOSE)))
    off = Harness(body, stored=creds(scopes=WITH_COMPOSE))
    async with off.egress:
        with pytest.raises(GoogleReauthRequired):
            await off.auth.access_token()
    on = Harness(
        ok(scope=" ".join(sorted(WITH_COMPOSE))),
        stored=creds(scopes=WITH_COMPOSE),
        allow_compose=True,
    )
    async with on.egress:
        assert (await on.auth.access_token(required_scopes=[SCOPE_GCOMPOSE])).get_secret_value()


# ---- failure modes --------------------------------------------------------


async def test_no_stored_token_requires_authorisation_and_notifies() -> None:
    h = Harness()
    async with h.egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
    assert exc.value.code == "no_token" and h.fake.calls == 0
    assert [n.code for n in h.notices] == ["no_token"]
    assert "google_oauth.py" in h.notices[0].message


async def test_invalid_grant_needs_reauth_notifies_once_and_fails_fast() -> None:
    h = Harness(json_response(400, fixture("invalid_grant.json")), stored=creds())
    async with h.egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
        assert exc.value.code == "invalid_grant" and exc.value.needs_reauth
        for _ in range(3):  # no hammering Google with a dead refresh token
            with pytest.raises(GoogleReauthRequired):
                await h.auth.access_token()
    assert h.fake.calls == 1
    assert [n.code for n in h.notices] == ["invalid_grant"]
    assert h.sleeps.delays == []  # not retried


async def test_new_authorisation_clears_the_dead_state() -> None:
    h = Harness(json_response(400, fixture("invalid_grant.json")), ok(), stored=creds())
    async with h.egress:
        with pytest.raises(GoogleReauthRequired):
            await h.auth.access_token()
        await h.store.save(creds(refresh="1//0gBRANDNEWREFRESHTOKEN0000000000000"))
        assert (await h.auth.access_token()).get_secret_value() == ACCESS_TOKEN
    assert h.fake.forms[1]["refresh_token"] == "1//0gBRANDNEWREFRESHTOKEN0000000000000"


async def test_notifier_failure_never_masks_the_auth_error() -> None:
    h = Harness(json_response(400, fixture("invalid_grant.json")), stored=creds())

    async def boom(notice: ReauthNotice) -> None:
        raise RuntimeError("telegram down")

    h.auth = GoogleAuth(h.client, h.store, notifier=boom, clock=h.clock)
    async with h.egress:
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
    assert exc.value.code == "invalid_grant"


async def test_invalid_client_is_fixed_code_not_retried_not_reauth() -> None:
    h = Harness(json_response(401, fixture("invalid_client.json")), stored=creds())
    async with h.egress:
        with pytest.raises(GoogleAuthError) as exc:
            await h.auth.access_token()
    assert exc.value.code == "invalid_client" and not isinstance(exc.value, GoogleReauthRequired)
    assert h.fake.calls == 1 and h.sleeps.delays == []


async def test_unknown_4xx_error_maps_to_generic_code_without_echoing_description() -> None:
    body = {"error": "something_new", "error_description": "leaky " + REFRESH_TOKEN}
    h = Harness(json_response(400, body), stored=creds())
    async with h.egress:
        with pytest.raises(GoogleAuthError) as exc:
            await h.auth.access_token()
    assert exc.value.code == "token_endpoint_rejected"
    assert REFRESH_TOKEN not in str(exc.value)


async def test_transient_5xx_is_retried_with_exponential_backoff_then_succeeds() -> None:
    boom = json_response(503, fixture("server_error.json"))
    h = Harness(boom, boom, ok(), stored=creds())
    async with h.egress:
        token = await h.auth.access_token()
    assert token.get_secret_value() == ACCESS_TOKEN
    assert h.fake.calls == 3
    assert h.sleeps.delays == [0.5, 1.0]


async def test_persistent_failure_stops_after_max_retries() -> None:
    boom = json_response(503, fixture("server_error.json"))
    h = Harness(boom, boom, boom, stored=creds(), max_retries=2)
    async with h.egress:
        with pytest.raises(GoogleAuthError) as exc:
            await h.auth.access_token()
    assert exc.value.code == "server_error" and not isinstance(exc.value, GoogleReauthRequired)
    assert h.fake.calls == 3  # 1 + 2 retries, then it gives up
    assert len(h.sleeps.delays) == 2


async def test_429_is_retried() -> None:
    h = Harness(json_response(429, {"error": "rate_limited"}), ok(), stored=creds())
    async with h.egress:
        await h.auth.access_token()
    assert h.fake.calls == 2


async def test_backoff_delay_is_capped_and_jittered_within_bounds() -> None:
    boom = json_response(500, {})
    fake = FakeTokenEndpoint(*([boom] * 6))
    sleeps = Sleeps()
    async with fake.egress() as egress:
        client = GoogleTokenClient(
            auth_settings(max_retries=5, backoff_base_s=1, backoff_max_s=4),
            egress,
            sleep=sleeps,
            jitter=lambda: 0.0,
        )
        with pytest.raises(GoogleAuthError):
            await client.refresh(creds().refresh_token)
    assert sleeps.delays == [
        0.5,
        1.0,
        2.0,
        2.0,
        2.0,
    ]  # base*2^n capped at 4, then halved (jitter 0)


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("reset")],
)
async def test_network_errors_are_retried_then_fixed_code(exc: Exception) -> None:
    h = Harness(exc, exc, exc, stored=creds())
    async with h.egress:
        with pytest.raises(GoogleAuthError) as err:
            await h.auth.access_token()
    assert err.value.code == "network_error"
    assert h.fake.calls == 3 and not isinstance(err.value, GoogleReauthRequired)


async def test_network_error_then_recovery() -> None:
    h = Harness(httpx.ConnectError("boom"), ok(), stored=creds())
    async with h.egress:
        assert (await h.auth.access_token()).get_secret_value() == ACCESS_TOKEN


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json=["list"]),
        httpx.Response(200, json={"expires_in": 3599, "scope": SCOPE_CAL}),
        httpx.Response(200, json={"access_token": "", "expires_in": 3599, "scope": SCOPE_CAL}),
        httpx.Response(200, json={"access_token": "t", "expires_in": "soon", "scope": SCOPE_CAL}),
        httpx.Response(200, json={"access_token": "t", "expires_in": -5, "scope": SCOPE_CAL}),
        httpx.Response(200, json={"access_token": "t", "expires_in": 3599, "scope": 7}),
    ],
)
async def test_malformed_success_responses_are_bad_response_not_retried(
    response: httpx.Response,
) -> None:
    h = Harness(response, stored=creds())
    async with h.egress:
        with pytest.raises(GoogleAuthError) as exc:
            await h.auth.access_token()
    assert exc.value.code == "bad_response" and h.fake.calls == 1


async def test_egress_denied_is_fixed_code_not_retried() -> None:
    transport = RecordingTransport()
    egress = EgressClient(
        NetSettings(_env_file=None, enabled_services=["telegram"]), transport=transport
    )
    client = GoogleTokenClient(auth_settings(), egress, sleep=Sleeps(), jitter=lambda: 1.0)
    auth = GoogleAuth(client, InMemoryTokenStore(creds()))
    async with egress:
        with pytest.raises(GoogleAuthError) as exc:
            await auth.access_token()
    assert exc.value.code == "egress_denied" and transport.calls == []


async def test_missing_client_credentials_fail_closed_before_any_request() -> None:
    fake = FakeTokenEndpoint()
    async with fake.egress() as egress:
        client = GoogleTokenClient(
            auth_settings(client_id=None, client_secret=None), egress, sleep=Sleeps()
        )
        auth = GoogleAuth(client, InMemoryTokenStore(creds()))
        with pytest.raises(GoogleAuthError) as exc:
            await auth.access_token()
    assert exc.value.code == "client_not_configured" and fake.calls == 0


# ---- revoke ---------------------------------------------------------------


async def test_revoke_calls_endpoint_and_forgets_the_token() -> None:
    h = Harness(json_response(200, {}), stored=creds())
    async with h.egress:
        await h.auth.revoke_and_forget()
        assert h.fake.paths == ["/revoke"]
        assert h.fake.forms[0]["token"] == REFRESH_TOKEN
        assert await h.store.load() is None
        with pytest.raises(GoogleReauthRequired) as exc:
            await h.auth.access_token()
    assert exc.value.code == "no_token"


async def test_revoke_of_already_revoked_token_is_success() -> None:
    h = Harness(json_response(400, {"error": "invalid_token"}), stored=creds())
    async with h.egress:
        await h.auth.revoke_and_forget()
        assert await h.store.load() is None


async def test_revoke_network_failure_still_forgets_locally_and_reports() -> None:
    h = Harness(
        httpx.ConnectError("x"), httpx.ConnectError("x"), httpx.ConnectError("x"), stored=creds()
    )
    async with h.egress:
        with pytest.raises(GoogleAuthError) as exc:
            await h.auth.revoke_and_forget()
        assert exc.value.code == "revoke_failed"
        assert await h.store.load() is None


class _UnreadableStore(InMemoryTokenStore):
    """Behaves like PostgresTokenStore on a record it cannot decrypt (an
    out-of-policy but readable record IS revoked: test_auth_followups.py)."""

    async def load(self) -> StoredCredentials | None:
        raise GoogleReauthRequired("scope_violation")

    async def load_for_revoke(self) -> StoredCredentials | None:
        raise GoogleAuthError("token_decrypt_failed")


async def test_revoke_of_unreadable_record_forgets_it_and_reports_manual_revoke() -> None:
    h = Harness()
    store = _UnreadableStore(creds())
    auth = GoogleAuth(h.client, store, clock=h.clock)
    async with h.egress:
        with pytest.raises(GoogleAuthError) as exc:
            await auth.revoke_and_forget()
    assert exc.value.code == "revoke_failed"
    assert store._creds is None
    assert h.fake.calls == 0


async def test_status_reports_scopes_never_secrets() -> None:
    h = Harness(stored=creds(scopes=frozenset({SCOPE_CAL})))
    st = await h.auth.status()
    assert st.has_token and st.scopes == frozenset({SCOPE_CAL}) and st.scope_ok
    assert REFRESH_TOKEN not in repr(st)
    assert not (await Harness().auth.status()).has_token


# ---- MED-4: fixed-code, secret-free audit/log lines -----------------------

LOGGER = "nimbus.integrations.google.auth"


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def _assert_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    blob = " ".join(repr(r.__dict__) + r.getMessage() for r in caplog.records)
    for canary in CANARIES:
        assert canary not in blob


async def test_invalid_grant_transition_is_logged_once(
    caplog: pytest.LogCaptureFixture, scrubbing: object
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    h = Harness(json_response(400, fixture("invalid_grant.json")), stored=creds())
    async with h.egress:
        for _ in range(3):
            with pytest.raises(GoogleReauthRequired):
                await h.auth.access_token()
    lines = [m for m in _lines(caplog) if "reauth_required" in m]
    assert lines == ["google auth reauth_required code=invalid_grant"]
    _assert_no_secrets(caplog)


async def test_scope_violation_on_refresh_and_on_load_are_logged(
    caplog: pytest.LogCaptureFixture, scrubbing: object
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    h = Harness(ok(scope=f"{SCOPE_CAL} {SCOPE_SEND}"), stored=creds())
    async with h.egress:
        with pytest.raises(GoogleReauthRequired):
            await h.auth.access_token()
    h2 = Harness(stored=creds(scopes=frozenset({SCOPE_CAL, SCOPE_SEND})))
    async with h2.egress:
        with pytest.raises(GoogleReauthRequired):
            await h2.auth.access_token()
    lines = [m for m in _lines(caplog) if "scope_violation" in m]
    assert lines == [
        "google auth reauth_required code=scope_violation",
        "google auth reauth_required code=scope_violation",
    ]
    _assert_no_secrets(caplog)


async def test_revoke_is_logged(caplog: pytest.LogCaptureFixture, scrubbing: object) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    h = Harness(json_response(200, {}), stored=creds())
    async with h.egress:
        await h.auth.revoke_and_forget()
    assert "google oauth token revoked and forgotten" in _lines(caplog)
    _assert_no_secrets(caplog)


# ---- INV-7 ----------------------------------------------------------------


def _blob(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc)) + repr(exc) + str(exc)


@pytest.fixture
def scrubbing() -> object:
    restore = install_log_scrubbing(secrets=CANARIES)
    yield None
    restore()


async def test_no_secret_in_exceptions_tracebacks_or_logs_across_failure_modes(
    caplog: pytest.LogCaptureFixture, scrubbing: object
) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = {
        "error": "invalid_grant",
        "error_description": f"bad {REFRESH_TOKEN} {ACCESS_TOKEN} {CLIENT_SECRET} {AUTH_CODE}",
    }
    scenarios: list[list[httpx.Response | Exception]] = [
        [json_response(400, leaky)],
        [json_response(401, {**leaky, "error": "invalid_client"})],
        [json_response(503, leaky)] * 3,
        [httpx.ConnectError(f"conn {REFRESH_TOKEN} {CLIENT_SECRET}")] * 3,
        [httpx.Response(200, content=f"garbage {ACCESS_TOKEN}".encode())],
        [ok(scope=f"{SCOPE_CAL} {SCOPE_SEND}")],
    ]
    for responses in scenarios:
        h = Harness(*responses, stored=creds())
        async with h.egress:
            with pytest.raises(GoogleAuthError) as exc:
                await h.auth.access_token()
        text = _blob(exc.value)
        for canary in CANARIES:
            assert canary not in text
        assert exc.value.__cause__ is None
        for notice in h.notices:
            assert not any(c in notice.message for c in CANARIES)
    # nothing in our own log output either (checked on the raw records, i.e.
    # independent of the scrubbing filter doing the work)
    for record in caplog.records:
        rendered = record.getMessage() + " " + repr(record.__dict__)
        for canary in CANARIES:
            assert canary not in rendered


def test_credentials_and_settings_reprs_hide_secrets() -> None:
    c = creds()
    s = auth_settings()
    for obj in (c, s, c.refresh_token):
        for text in (repr(obj), str(obj)):
            assert REFRESH_TOKEN not in text and CLIENT_SECRET not in text


def test_settings_validation_error_does_not_echo_secret() -> None:
    with pytest.raises(ValidationError) as exc:
        auth_settings(client_secret=CLIENT_SECRET, max_retries="not-an-int-" + CLIENT_SECRET)
    assert CLIENT_SECRET not in str(exc.value)
