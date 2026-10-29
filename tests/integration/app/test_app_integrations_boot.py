# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`run_app` wiring (P3.0 MED-1, H25/H26): the orchestrator always gets the
quarantine lane with the configured per-turn budget, and the enabled scopes
are exactly `LOCAL_SCOPES` when no Google/Notion credentials are configured
(boot unchanged). Real Postgres; fake Telegram; no Google/Notion traffic."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from src.app import runtime as rt
from src.integrations.google.auth import GoogleOAuthSettings
from src.integrations.notion.settings import NotionSettings
from src.lanes.quarantine import Quarantine
from src.orchestrator.loop import Orchestrator
from src.policy import LOCAL_SCOPES
from tests.integration.app.harness import (
    PacedTelegram,
    backends_for,
    make_cfg,
    run_until,
)
from tests.unit.gateway.fake_telegram import make_egress, message

BASE = 7_100_000_000


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    class Spy(Orchestrator):
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen.append({**kw, "_tools": a[1]})
            super().__init__(*a, **kw)

    monkeypatch.setattr(rt, "Orchestrator", Spy)
    return seen


def with_egress(fake: PacedTelegram) -> Any:
    """Like the production factory: real (fake-transport) egress handed over."""
    inner = backends_for(fake)

    def factory(cfg: Any, sf: Any, beat: Any) -> Any:
        be = inner(cfg, sf, beat)
        be.egress = make_egress(fake)
        be.closers.append(be.egress.aclose)
        return be

    return factory


def _with(cfg: rt.AppConfig, **kw: Any) -> rt.AppConfig:
    return rt.AppConfig.build(
        app=cfg.app, gateway=cfg.gateway, llm=cfg.llm, net=cfg.net, dsn=cfg.dsn, **kw
    )


async def test_boot_without_credentials_wires_quarantine_and_local_scopes_only(
    tmp_path: Path, captured: list[dict[str, Any]]
) -> None:
    fake = PacedTelegram()
    fake.push(message(BASE + 1, "hello"))
    cfg = make_cfg(tmp_path, quarantine_calls_per_turn=3)
    code = await run_until(cfg, with_egress(fake), lambda: bool(fake.sent))
    assert code == rt.EXIT_OK and fake.sent
    (kw,) = captured
    assert isinstance(kw["quarantine"], Quarantine)
    assert kw["config"].max_quarantine_calls_per_turn == 3
    assert kw["scopes_enabled"] == LOCAL_SCOPES
    available = {t.spec.name.split("_")[0] for t in kw["_tools"] if t.available()}
    assert available.isdisjoint({"calendar", "drive", "notion"})


async def test_google_configured_but_not_authorised_boots_with_google_off(
    tmp_path: Path, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PostgresTokenStore (empty) under a real data key: Google stays off,
    Notion (token set) comes on, and the app still serves turns."""
    monkeypatch.setenv("NIMBUS_DATA_KEY", base64.b64encode(b"k" * 32).decode())
    fake = PacedTelegram()
    fake.push(message(BASE + 2, "hello"))
    cfg = _with(
        make_cfg(tmp_path),
        google=GoogleOAuthSettings(_env_file=None, client_id="cid", client_secret="csecret"),
        notion=NotionSettings(_env_file=None, token="ntn_boot_test_token"),
    )
    code = await run_until(cfg, with_egress(fake), lambda: bool(fake.sent))
    assert code == rt.EXIT_OK and fake.sent
    (kw,) = captured
    assert kw["scopes_enabled"] == LOCAL_SCOPES | {"notion"}


async def test_fake_backends_never_bind_integrations(
    tmp_path: Path, captured: list[dict[str, Any]]
) -> None:
    fake = PacedTelegram()
    fake.push(message(BASE + 3, "hello"))
    cfg = _with(make_cfg(tmp_path), notion=NotionSettings(_env_file=None, token="ntn_x"))
    await run_until(cfg, backends_for(fake), lambda: bool(fake.sent))
    (kw,) = captured
    assert kw["scopes_enabled"] == LOCAL_SCOPES
