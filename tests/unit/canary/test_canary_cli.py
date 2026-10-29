# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.4 `python -m src.app canary-sweep`: exit codes and output hygiene. The
database part is faked here (the real sweep runs in tests/integration/canary)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from src.app import cli
from src.app import runtime as rt
from src.app import sweep as sweep_cli
from src.app.settings import AppSettings
from src.canary import sweep as canary_sweep_mod
from src.canary.settings import CanarySettings
from src.canary.sweep import SweepFinding, SweepReport
from src.net import NetSettings
from src.net.http import _outbound_text
from src.net.policy import Service
from tests._canary_helpers import SEED
from tests.fakes.llm import llm_settings
from tests.unit.gateway.fake_telegram import TOKEN, make_settings


def _cfg(tmp_path: Path, seed: str | None) -> rt.AppConfig:
    cs = CanarySettings(_env_file=None, seed=SecretStr(seed) if seed else None)  # type: ignore[call-arg]
    return rt.AppConfig.build(
        app=AppSettings(_env_file=None, heartbeat_path=tmp_path / "hb.json"),
        gateway=make_settings(),
        llm=llm_settings(),
        net=NetSettings(_env_file=None),
        dsn="postgresql+asyncpg://u:p@127.0.0.1:1/none",
        canary=cs,
    )


class _Engine:
    async def dispose(self) -> None:
        return None


class _Session:
    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    async def __aenter__(self) -> _Session:  # noqa: PYI034
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


def _fake_db(monkeypatch: pytest.MonkeyPatch, report: SweepReport | Exception) -> list[Any]:
    seen: list[Any] = []

    async def fake_sweep(session: Any, detector: Any, **kw: Any) -> SweepReport:
        seen.append(kw)
        if isinstance(report, Exception):
            raise report
        return report

    monkeypatch.setattr(canary_sweep_mod, "sweep_database", fake_sweep)
    monkeypatch.setattr("src.app.db.build_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession", _Session)
    return seen


async def test_no_seed_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt.AppConfig, "from_env", classmethod(lambda c: _cfg(tmp_path, None)))
    out: list[str] = []
    assert await sweep_cli.canary_sweep(out.append) == 2
    assert "CANARY_SEED" in out[0]


async def test_invalid_settings_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cls: Any) -> Any:
        raise ValueError("bad")

    monkeypatch.setattr(rt.AppConfig, "from_env", classmethod(boom))
    out: list[str] = []
    assert await sweep_cli.canary_sweep(out.append) == 2 and "invalid settings" in out[0]


@pytest.mark.parametrize(
    ("report", "code"),
    [
        (SweepReport(3, 4, 10), 0),
        (SweepReport(3, 4, 10, (SweepFinding("event", "body", "canary", 2),)), 1),
        (RuntimeError(f"db said {TOKEN}"), 2),
    ],
)
async def test_exit_codes_and_secret_free_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: Any, code: int
) -> None:
    cfg = _cfg(tmp_path, SEED)
    monkeypatch.setattr(rt.AppConfig, "from_env", classmethod(lambda c: cfg))
    seen = _fake_db(monkeypatch, report)
    out: list[str] = []
    assert await sweep_cli.canary_sweep(out.append) == code
    text = "\n".join(out)
    assert TOKEN not in text and SEED not in text
    assert not any(v in text for v in cfg.canaries.values())
    if seen:
        literals = seen[0]["literals"]
        assert TOKEN in literals and not set(cfg.canaries.values()) & set(literals)


def test_cli_dispatches_the_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(out: Any = print) -> int:
        return 1

    monkeypatch.setattr(sweep_cli, "canary_sweep", fake)
    assert cli.main(["canary-sweep"]) == 1


def test_outbound_text_never_includes_the_telegram_path() -> None:
    blob = _outbound_text(Service.TELEGRAM, f"/bot{TOKEN}/sendMessage", None, {"text": "hi"}, None)
    assert TOKEN not in blob and "hi" in blob
    blob2 = _outbound_text(Service.NOTION, "/v1/pages/x", {"q": "1"}, None, b"body")
    assert "/v1/pages/x" in blob2 and "body" in blob2 and "'q'" in blob2
