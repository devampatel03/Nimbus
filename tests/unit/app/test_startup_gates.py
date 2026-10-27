# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Startup order and refusals that need no database: scrubbing first (R12),
card gate (R9), dev-only switches. `run_app` returns before touching Postgres
or the network in every case here."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.app import runtime as rt
from src.app.runtime import EXIT_CONFIG, AppConfig, Backends, run_app
from src.app.settings import AppSettings
from src.llm.settings import DEFAULT_API_KEY, LLMSettings
from src.net import NetSettings
from tests.fakes.llm import API_KEY, llm_settings
from tests.unit.gateway.fake_telegram import TOKEN, make_settings

CANARY = "CANARY-7f3a91c2d4e8b605"


def make_cfg(tmp_path: Path, **app: Any) -> AppConfig:
    app_s = AppSettings(_env_file=None, heartbeat_path=tmp_path / "hb.json", **app)
    return AppConfig.build(
        app=app_s,
        gateway=make_settings(),
        llm=llm_settings(canaries=(CANARY,)),
        net=NetSettings(_env_file=None),
        dsn="postgresql+asyncpg://u:p@127.0.0.1:1/none",
    )


@pytest.fixture(autouse=True)
def _restore_log_scrubbing(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record the order of startup steps and undo the process-wide scrubber."""
    order: list[str] = []
    restores: list[Any] = []
    real_install = rt.install_log_scrubbing

    def spy_install(secrets: Any = ()) -> Any:
        order.append("scrub")
        restores.append(real_install(secrets))
        return restores[-1]

    monkeypatch.setattr(rt, "install_log_scrubbing", spy_install)
    monkeypatch.delenv("NIMBUS_CARD_DEV_MODE", raising=False)
    yield order
    for undo in restores:
        undo()


def never_called(order: list[str]) -> Any:
    def factory(*_: Any, **__: Any) -> Backends:
        order.append("backends")
        raise AssertionError("backends must not be built")

    return factory


async def test_scrubbing_is_installed_first_with_every_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _restore_log_scrubbing: list[str]
) -> None:
    order = _restore_log_scrubbing
    captured: list[tuple[str, ...]] = []
    real = rt.install_log_scrubbing

    def spy(secrets: Any = ()) -> Any:
        captured.append(tuple(secrets))
        return real(secrets)

    monkeypatch.setattr(rt, "install_log_scrubbing", spy)
    real_card = rt.load_card

    def card_spy(env: str, **kw: Any) -> str:
        order.append("card")
        return real_card(env, **kw)

    monkeypatch.setattr(rt, "load_card", card_spy)
    cfg = make_cfg(tmp_path)
    assert await run_app(cfg, backends=never_called(order)) == EXIT_CONFIG
    assert order[:2] == ["scrub", "card"]  # scrubbing before the very first startup step
    secrets = captured[0]
    assert TOKEN in secrets and API_KEY in secrets and CANARY in secrets


async def test_after_startup_a_secret_in_any_log_line_is_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = make_cfg(tmp_path)
    await run_app(cfg, backends=never_called([]))  # refuses at the card gate; scrubber is live
    with caplog.at_level(logging.INFO):
        logging.getLogger("some.other.module").error("token=%s canary=%s", TOKEN, CANARY)
        logging.getLogger("x").warning("path /bot%s/getMe", TOKEN, extra={"detail": TOKEN})
    text = "\n".join(
        r.getMessage() + " ".join(str(v) for k, v in r.__dict__.items() if k == "detail")
        for r in caplog.records
    )
    assert TOKEN not in text and CANARY not in text
    assert "REDACTED" in text


async def test_unapproved_card_refuses_to_start_before_any_backend(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    order: list[str] = []
    with caplog.at_level(logging.CRITICAL):
        code = await run_app(make_cfg(tmp_path, env="production"), backends=never_called(order))
    assert code == EXIT_CONFIG and "backends" not in order
    assert any("character card" in r.getMessage() for r in caplog.records)


async def test_production_refuses_the_card_dev_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIMBUS_CARD_DEV_MODE", "1")
    order: list[str] = []
    code = await run_app(make_cfg(tmp_path, env="production"), backends=never_called(order))
    assert code == EXIT_CONFIG and "backends" not in order


async def test_default_environment_is_production(tmp_path: Path) -> None:
    assert AppSettings(_env_file=None).env == "production"


async def test_fake_backends_are_refused_in_production_even_with_an_approved_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must stand on its own: make the card gate pass so only the
    fake-backend rule can stop the process."""
    monkeypatch.setattr(rt, "load_card", lambda env, **kw: "approved card")
    order: list[str] = []
    cfg = make_cfg(tmp_path, env="production", fake_backends=True)
    assert await run_app(cfg, backends=never_called(order)) == EXIT_CONFIG
    assert "backends" not in order  # never built


async def test_unsafe_heartbeat_location_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.app.heartbeat import Heartbeat, HeartbeatPathError

    def refuse(self: Heartbeat) -> None:
        raise HeartbeatPathError("heartbeat directory is world-writable")

    monkeypatch.setattr(Heartbeat, "prepare", refuse)
    monkeypatch.setenv("NIMBUS_CARD_DEV_MODE", "1")
    order: list[str] = []
    assert await run_app(make_cfg(tmp_path, env="development"), backends=never_called(order)) == (
        EXIT_CONFIG
    )
    assert "backends" not in order


def test_secret_list_has_token_llm_key_canaries_but_not_placeholders(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    assert set(cfg.secrets) >= {TOKEN, API_KEY, CANARY}
    plain = AppConfig.build(
        app=AppSettings(_env_file=None),
        gateway=None,
        llm=LLMSettings(_env_file=None),  # type: ignore[call-arg]
        net=NetSettings(_env_file=None),
        dsn="x",
    )
    assert DEFAULT_API_KEY not in plain.secrets


def test_config_repr_does_not_show_secrets(tmp_path: Path) -> None:
    cfg = AppConfig.build(
        app=AppSettings(_env_file=None),
        gateway=make_settings(),
        llm=llm_settings(),
        net=NetSettings(_env_file=None),
        dsn="postgresql+asyncpg://u:DBPASSWORD123@127.0.0.1:1/none",  # pragma: allowlist secret
    )
    shown = repr(cfg)
    assert TOKEN not in shown and API_KEY not in shown and "DBPASSWORD123" not in shown
    assert "DBPASSWORD123" in cfg.dsn  # still usable, just not printable


def test_app_settings_validation_error_hides_values() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as ei:
        AppSettings(_env_file=None, heartbeat_stale_s="SUPERSECRETVALUE")  # type: ignore[arg-type]
    assert "SUPERSECRETVALUE" not in str(ei.value)


def test_api_embedder_option_shares_the_llm_accountant(tmp_path: Path) -> None:
    from tests.fakes.llm import build_env

    cfg = make_cfg(tmp_path, embedder="api")
    env = build_env()
    emb = rt.build_embedder(cfg, env.llm)
    assert emb._acct is env.llm.accountant  # type: ignore[attr-defined]


def test_db_password_and_data_key_are_in_the_scrub_list_via_the_real_build_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `dsn=` shortcut: this is the path `from_env` takes."""
    from config.settings import Settings as DbSettings

    monkeypatch.setenv("NIMBUS_DATA_KEY", "data-key-value-abcdef0123456789")
    cfg = AppConfig.build(
        app=AppSettings(_env_file=None),
        gateway=make_settings(),
        llm=llm_settings(),
        net=NetSettings(_env_file=None),
        db=DbSettings(_env_file=None, postgres_password="db-password-XYZ-98765"),  # type: ignore[arg-type]
    )
    assert "db-password-XYZ-98765" in cfg.secrets  # pragma: allowlist secret
    assert "data-key-value-abcdef0123456789" in cfg.secrets
    assert "db-password-XYZ-98765" in cfg.dsn  # and the DSN is built from the same settings


async def test_real_backends_wire_the_gateway_liveness_beat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production factory must hand the heartbeat to the gateway: without it
    the gateway loop could never beat and the process would read as unhealthy."""
    from tests.unit.gateway.fake_store import MemStore
    from tests.unit.gateway.fake_telegram import FakeTelegram, make_egress

    fake = FakeTelegram()
    monkeypatch.setattr(rt, "EgressClient", lambda net: make_egress(fake))
    monkeypatch.setattr(rt, "UpdateStore", lambda sf: MemStore())
    beats: list[int] = []
    be = rt.real_backends(make_cfg(tmp_path), None, lambda: beats.append(1))  # type: ignore[arg-type]

    async def one_poll() -> None:
        async for _ in be.gateway.inbound():
            pass

    task = asyncio.create_task(one_poll())
    for _ in range(100):
        if beats:
            break
        await asyncio.sleep(0.01)
    await be.gateway.aclose()  # type: ignore[attr-defined]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert beats, "real gateway never signalled a completed poll"
