# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""H25/H26/P3.4 wiring: Google (Calendar, Drive) and Notion tool groups are
bound, and their scopes enabled, ONLY when their credentials are configured.
Absent credentials: the groups stay hidden and the scopes are exactly
`LOCAL_SCOPES` (the app boots as before). The new secrets reach the log
scrubber. No network is touched here (fake transport, in-memory token store)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.app import integrations as integ
from src.app.fakes import InMemoryGateway
from src.app.runtime import AppConfig
from src.app.settings import AppSettings
from src.core.clock import SystemClock
from src.integrations.google.auth import (
    SCOPE_CALENDAR_EVENTS,
    SCOPE_DRIVE_READONLY,
    SCOPE_GMAIL_COMPOSE,
    SCOPE_GMAIL_READONLY,
    GoogleAuthError,
    GoogleOAuthSettings,
    InMemoryTokenStore,
    ReauthNotice,
    StoredCredentials,
)
from src.integrations.notion.settings import NotionSettings
from src.lanes.quarantine import MAX_QUARANTINE_CALLS_PER_TURN, QUARANTINE_CALLS_RANGE
from src.net import EgressClient, NetSettings
from src.policy import LOCAL_SCOPES
from src.tools.registry import ToolRegistry
from tests.fakes.llm import llm_settings
from tests.unit.gateway.fake_telegram import make_settings

CLIENT_ID = "client-id-7c1e.apps.example"
CLIENT_SECRET = "client-secret-91ab-XYZ"  # pragma: allowlist secret
NOTION_SECRET = "ntn_secret_value_3f2a77"  # pragma: allowlist secret
REFRESH = "1//refresh-token-value-55aa"  # pragma: allowlist secret
EXTERNAL = ("calendar", "drive", "notion")


class NoNetwork(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("wiring must not touch the network")


def egress() -> EgressClient:
    return EgressClient(NetSettings(_env_file=None), transport=NoNetwork())


def google(**kw: Any) -> GoogleOAuthSettings:
    base: dict[str, Any] = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    base.update(kw)
    return GoogleOAuthSettings(_env_file=None, **base)


def notion(token: str | None = NOTION_SECRET) -> NotionSettings:
    return NotionSettings(_env_file=None, token=token)


def creds(*scopes: str) -> StoredCredentials:
    return StoredCredentials(SecretStr(REFRESH), frozenset(scopes), datetime.now(UTC))


class BrokenStore(InMemoryTokenStore):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    async def load(self) -> StoredCredentials | None:
        raise self._exc


@pytest.fixture(autouse=True)
def scrub_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    added: list[str] = []
    monkeypatch.setattr(integ, "install_log_scrubbing", lambda s=(): added.extend(s))
    return added


def offered(reg: ToolRegistry) -> set[str]:
    return {t.spec.name for t in reg.loop_tools() if getattr(t, "available", lambda: True)()}


async def wire(reg: ToolRegistry, **kw: Any) -> integ.WiredIntegrations:
    args: dict[str, Any] = {
        "egress": egress(),
        "sf": None,
        "gateway": InMemoryGateway(),
        "clock": SystemClock(),
        "google": None,
        "notion": None,
    }
    args.update(kw)
    return await integ.wire_integrations(reg, **args)


def external_offered(reg: ToolRegistry) -> set[str]:
    return {n for n in offered(reg) if n.split("_")[0] in EXTERNAL}


# --------------------------------------------------------------- absent creds


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"google": GoogleOAuthSettings(_env_file=None), "notion": NotionSettings(_env_file=None)},
        {"google": google(client_secret=""), "notion": notion("")},
        {"google": google(client_id=None)},
    ],
    ids=["none", "unset", "empty", "half"],
)
async def test_no_credentials_boot_is_unchanged(kw: dict[str, Any], scrub_spy: list[str]) -> None:
    reg = ToolRegistry()
    wired = await wire(reg, **kw)
    assert wired.scopes == LOCAL_SCOPES and wired.google_auth is None
    assert external_offered(reg) == set()
    assert scrub_spy == []


async def test_no_egress_binds_nothing_even_with_credentials() -> None:
    reg = ToolRegistry()
    store = InMemoryTokenStore(creds(SCOPE_CALENDAR_EVENTS, SCOPE_DRIVE_READONLY))
    wired = await wire(reg, egress=None, google=google(), notion=notion(), google_store=store)
    assert wired.scopes == LOCAL_SCOPES and external_offered(reg) == set()


# --------------------------------------------------------------- notion


async def test_notion_token_binds_notion_only() -> None:
    reg = ToolRegistry()
    wired = await wire(reg, notion=notion())
    assert wired.scopes == LOCAL_SCOPES | {"notion"}
    assert external_offered(reg) == {"notion_search", "notion_read", "notion_append"}


# --------------------------------------------------------------- google


@pytest.mark.parametrize(
    ("scopes", "enabled"),
    [
        ((SCOPE_CALENDAR_EVENTS,), {"calendar"}),
        ((SCOPE_DRIVE_READONLY,), {"drive"}),
        ((SCOPE_CALENDAR_EVENTS, SCOPE_DRIVE_READONLY), {"calendar", "drive"}),
        ((SCOPE_GMAIL_READONLY,), set()),  # Gmail is not wired (H16)
    ],
)
async def test_google_scope_enabled_only_for_granted_bound_groups(
    scopes: tuple[str, ...], enabled: set[str], scrub_spy: list[str]
) -> None:
    reg = ToolRegistry()
    wired = await wire(reg, google=google(), google_store=InMemoryTokenStore(creds(*scopes)))
    assert wired.scopes == LOCAL_SCOPES | enabled
    assert {n.split("_")[0] for n in external_offered(reg)} == enabled
    assert wired.google_auth is not None
    assert REFRESH in scrub_spy  # the stored refresh token is scrubbed from logs


async def test_google_not_authorised_stays_off() -> None:
    reg = ToolRegistry()
    wired = await wire(reg, google=google(), google_store=InMemoryTokenStore(None))
    assert wired.scopes == LOCAL_SCOPES and external_offered(reg) == set()


async def test_google_out_of_policy_grant_stays_off(caplog: pytest.LogCaptureFixture) -> None:
    reg = ToolRegistry()
    store = InMemoryTokenStore(creds(SCOPE_CALENDAR_EVENTS, "https://mail.google.com/"))
    with caplog.at_level(logging.INFO):
        wired = await wire(reg, google=google(), google_store=store)
    assert wired.scopes == LOCAL_SCOPES and wired.google_auth is None
    assert "scope_violation" in caplog.text and "mail.google.com" not in caplog.text


async def test_compose_grant_needs_the_same_flag_as_the_client() -> None:
    reg = ToolRegistry()
    store = InMemoryTokenStore(creds(SCOPE_CALENDAR_EVENTS, SCOPE_GMAIL_COMPOSE))
    off = await wire(reg, google=google(), google_store=store)
    assert off.scopes == LOCAL_SCOPES
    reg2 = ToolRegistry()
    store_on = InMemoryTokenStore(
        creds(SCOPE_CALENDAR_EVENTS, SCOPE_GMAIL_COMPOSE), allow_compose=True
    )
    on = await wire(reg2, google=google(allow_gmail_compose=True), google_store=store_on)
    assert on.scopes == LOCAL_SCOPES | {"calendar"}


async def test_google_auth_construction_failure_stays_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """E.g. a store/client compose-flag mismatch (refused by GoogleAuth once
    the P3.4 follow-up lands): Google stays off, boot continues."""

    def refuse(*a: Any, **k: Any) -> Any:
        raise GoogleAuthError("compose_flag_mismatch")

    monkeypatch.setattr(integ, "GoogleAuth", refuse)
    reg = ToolRegistry()
    store = InMemoryTokenStore(creds(SCOPE_CALENDAR_EVENTS))
    with caplog.at_level(logging.INFO):
        wired = await wire(reg, google=google(), google_store=store)
    assert wired.scopes == LOCAL_SCOPES and wired.google_auth is None
    assert "compose_flag_mismatch" in caplog.text


@pytest.mark.parametrize(
    "exc", [GoogleAuthError("token_decrypt_failed"), RuntimeError(f"boom {REFRESH}")]
)
async def test_unreadable_token_store_stays_off_and_logs_no_secret(
    exc: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    reg = ToolRegistry()
    with caplog.at_level(logging.INFO):
        wired = await wire(reg, google=google(), google_store=BrokenStore(exc))
    assert wired.scopes == LOCAL_SCOPES and external_offered(reg) == set()
    assert REFRESH not in caplog.text and CLIENT_SECRET not in caplog.text


async def test_google_without_data_key_stays_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIMBUS_DATA_KEY", raising=False)
    reg = ToolRegistry()
    wired = await wire(reg, google=google(), sf=object())
    assert wired.scopes == LOCAL_SCOPES


async def test_google_without_database_stays_off() -> None:
    reg = ToolRegistry()
    wired = await wire(reg, google=google(), sf=None)
    assert wired.scopes == LOCAL_SCOPES


async def test_reauth_notice_goes_to_the_owner_as_fixed_text() -> None:
    gw = InMemoryGateway()
    await integ._owner_notifier(gw)(ReauthNotice(code="invalid_grant", message="fixed text"))
    assert gw.sent == ["fixed text"]


# --------------------------------------------------------------- config / scrubbing


def _cfg(tmp_path: Path, **kw: Any) -> AppConfig:
    return AppConfig.build(
        app=AppSettings(_env_file=None, heartbeat_path=tmp_path / "hb.json"),
        gateway=make_settings(),
        llm=llm_settings(),
        net=NetSettings(_env_file=None),
        dsn="postgresql+asyncpg://u:p@127.0.0.1:1/none",
        **kw,
    )


def test_new_credentials_are_scrubber_secrets(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, google=google(), notion=notion())
    assert {CLIENT_ID, CLIENT_SECRET, NOTION_SECRET} <= set(cfg.secrets)
    assert NOTION_SECRET not in repr(cfg) and CLIENT_SECRET not in repr(cfg)


def test_absent_credentials_add_no_secrets(tmp_path: Path) -> None:
    base = _cfg(tmp_path)
    empty = _cfg(
        tmp_path, google=GoogleOAuthSettings(_env_file=None), notion=NotionSettings(_env_file=None)
    )
    assert base.secrets == empty.secrets
    assert "" not in empty.secrets


def test_app_setting_budget_matches_the_lane_constants() -> None:
    field = AppSettings.model_fields["quarantine_calls_per_turn"]
    assert field.default == MAX_QUARANTINE_CALLS_PER_TURN
    bounds = {type(m).__name__: m for m in field.metadata}
    assert bounds["Ge"].ge == QUARANTINE_CALLS_RANGE[0]
    assert bounds["Le"].le == QUARANTINE_CALLS_RANGE[1]


@pytest.mark.parametrize("bad", [-1, 26])
def test_app_setting_budget_is_bounded(bad: int) -> None:
    with pytest.raises(ValueError):
        AppSettings(_env_file=None, quarantine_calls_per_turn=bad)


async def test_real_backends_hand_over_the_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production factory: integrations need the egress; fakes never get one."""
    from src.app import runtime as rt
    from tests.unit.gateway.fake_store import MemStore

    sentinel = egress()
    monkeypatch.setattr(rt, "EgressClient", lambda net: sentinel)
    monkeypatch.setattr(rt, "UpdateStore", lambda sf: MemStore())
    be = rt.real_backends(_cfg(tmp_path), None, lambda: None)  # type: ignore[arg-type]
    assert be.egress is sentinel
    fake = rt.fake_backends(_cfg(tmp_path), None, lambda: None)  # type: ignore[arg-type]
    assert fake.egress is None
    for close in [*be.closers, *fake.closers]:
        await close()
