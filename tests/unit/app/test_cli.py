# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""CLI exit codes: 1 is reserved for "unhealthy"; any startup failure is 2."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from typing import Any

import pytest

from src.app import cli


def test_unexpected_config_exception_exits_2_with_type_name_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.app import runtime

    def boom() -> Any:
        raise RuntimeError("postgresql://u:HUNTERSECRET@host/db")  # pragma: allowlist secret

    monkeypatch.setattr(runtime.AppConfig, "from_env", staticmethod(boom))
    assert cli.run() == 2
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "HUNTERSECRET" not in err


def test_unexpected_startup_crash_exits_2_not_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.app import runtime

    async def boom(*a: Any, **k: Any) -> int:
        raise OSError("password=HUNTERSECRET")

    monkeypatch.setattr(runtime.AppConfig, "from_env", staticmethod(lambda: object()))
    monkeypatch.setattr(runtime, "run_app", boom)
    assert cli.run() == 2
    err = capsys.readouterr().err
    assert "OSError" in err and "HUNTERSECRET" not in err


def test_healthcheck_exit_1_still_means_unhealthy_only(tmp_path: Any) -> None:
    assert cli.healthcheck(tmp_path / "missing.json", 60) == 1


def test_signal_fallback_restores_the_previous_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Where loop.add_signal_handler is unavailable (Windows) the fallback uses
    signal.signal; `restore` must put the old handlers back."""
    from src.app.runtime import _install_signal_handlers

    sigs = (signal.SIGINT, signal.SIGTERM)

    def sentinel(*_: Any) -> None:  # pragma: no cover - never fired
        pass

    before = {s: signal.signal(s, sentinel) for s in sigs}

    async def go() -> None:
        loop = asyncio.get_running_loop()

        def refuse(*_a: Any, **_k: Any) -> None:
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", refuse)
        restore = _install_signal_handlers(asyncio.Event())
        assert all(signal.getsignal(s) is not sentinel for s in sigs)  # ours is installed
        restore()
        assert all(signal.getsignal(s) is sentinel for s in sigs)  # and now gone again

    try:
        asyncio.run(go())
    finally:
        for s, h in before.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(s, h)
    assert sys.platform  # keep the import used on every platform
