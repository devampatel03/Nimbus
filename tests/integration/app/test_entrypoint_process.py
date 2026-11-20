# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""`python -m src.app` as a real child process, against the in-process fakes
(`APP_FAKE_BACKENDS`): no token, no network. Proves the acceptance criteria:
it starts, heartbeats all three loops, `healthcheck` agrees, and goes
non-zero once the heartbeat is stale or the process is gone."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.integration.app import harness

ROOT = Path(__file__).resolve().parents[3]


def _env(tmp_path: Path) -> dict[str, str]:
    db = harness.CURRENT_DSN.rsplit("/", 1)[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "LLM_", "APP_"))}
    env.update(
        APP_ENV="development",
        APP_FAKE_BACKENDS="true",
        NIMBUS_CARD_DEV_MODE="1",
        APP_HEARTBEAT_PATH=str(tmp_path / "hb.json"),
        APP_HEARTBEAT_STALE_S="6",
        APP_RECOVER_INTERVAL_S="0.2",
        APP_SCHEDULER_TICK_S="0.2",
        POSTGRES_DB=db,
        PYTHONUNBUFFERED="1",
    )
    return env


def _healthcheck(env: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "src.app", "healthcheck", *extra],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_python_dash_m_src_app_starts_beats_and_healthcheck_tracks_it(tmp_path: Path) -> None:
    env = _env(tmp_path)
    hb = tmp_path / "hb.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 120
        loops: set[str] = set()
        while time.monotonic() < deadline and proc.poll() is None:
            if hb.exists():
                try:
                    loops = set(json.loads(hb.read_text(encoding="utf-8"))["loops"])
                except (ValueError, OSError, KeyError):
                    loops = set()
            if loops >= {"gateway", "worker", "scheduler"}:
                break
            time.sleep(0.3)
        assert loops >= {"gateway", "worker", "scheduler"}, f"did not start (rc={proc.poll()})"
        assert _healthcheck(env).returncode == 0

        if sys.platform == "win32":
            proc.kill()  # no POSIX signals; graceful stop is covered in-process
            proc.wait(timeout=30)
        else:
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=30) == 0  # graceful shutdown on SIGTERM
            assert not hb.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        proc.wait(timeout=30)

    time.sleep(1.2)
    stale = _healthcheck(env, "--stale-s", "1")
    assert stale.returncode == 1 and "unhealthy" in stale.stdout
    assert "HUNTER2" not in out


def test_production_start_with_unapproved_card_exits_2_without_serving(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.update(APP_ENV="production", APP_FAKE_BACKENDS="false")
    env.pop("NIMBUS_CARD_DEV_MODE")
    env.update(
        TELEGRAM_BOT_TOKEN="123456789:AAH_fake_token_for_tests_only_0123456789",
        TELEGRAM_ALLOWED_USER_ID="4242",
    )
    r = subprocess.run(
        [sys.executable, "-m", "src.app"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 2
    assert not (tmp_path / "hb.json").exists()
    assert "AAH_fake_token" not in r.stdout + r.stderr


def test_missing_telegram_settings_exit_2_names_fields_not_values(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.update(APP_ENV="production", APP_FAKE_BACKENDS="false")
    r = subprocess.run(
        [sys.executable, "-m", "src.app"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 2 and "bot_token" in r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_in_process_sets_stop() -> None:  # pragma: no cover - POSIX only
    import asyncio

    from src.app.runtime import _install_signal_handlers

    async def go() -> bool:
        stop = asyncio.Event()
        restore = _install_signal_handlers(stop)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(stop.wait(), 5)
            return stop.is_set()
        finally:
            restore()

    assert asyncio.run(go())
