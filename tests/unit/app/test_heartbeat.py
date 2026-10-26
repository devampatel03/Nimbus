# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Heartbeat file + healthcheck verdict (fail closed on anything odd)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.app.cli import healthcheck, main
from src.app.heartbeat import Heartbeat, check_heartbeat


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    clock = Clock()
    hb = Heartbeat(tmp_path / "hb.json", clock=clock)
    hb.register("gateway", "worker", "scheduler")
    clock.t += 10
    for name in ("gateway", "worker", "scheduler"):
        hb.beat(name)
    assert check_heartbeat(tmp_path / "hb.json", stale_s=60, now=clock.t + 5).ok


def test_one_stale_loop_makes_the_process_unhealthy(tmp_path: Path) -> None:
    clock = Clock()
    hb = Heartbeat(tmp_path / "hb.json", clock=clock)
    hb.register("gateway", "worker")
    clock.t += 100
    hb.beat("gateway")  # worker never beats again
    rep = check_heartbeat(tmp_path / "hb.json", stale_s=60, now=clock.t + 1)
    assert not rep.ok and "worker" in rep.detail


def test_registered_loop_that_never_beats_goes_stale(tmp_path: Path) -> None:
    clock = Clock()
    Heartbeat(tmp_path / "hb.json", clock=clock).register("gateway")
    assert not check_heartbeat(tmp_path / "hb.json", stale_s=60, now=clock.t + 61).ok


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "not json",
        "[]",
        "{}",
        '{"loops": {}}',
        '{"loops": {"a": "x"}}',
        '{"loops": {"a": true}}',
    ],
)
def test_missing_or_malformed_file_is_unhealthy(tmp_path: Path, content: str | None) -> None:
    p = tmp_path / "hb.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    assert not check_heartbeat(p, stale_s=60, now=2000.0).ok


def test_heartbeat_file_holds_only_names_and_times(tmp_path: Path) -> None:
    p = tmp_path / "hb.json"
    hb = Heartbeat(p, clock=Clock(), pid=7)
    hb.register("gateway")
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "pid": 7,
        "started": 1000.0,
        "loops": {"gateway": 1000.0},
    }
    assert not p.with_name("hb.json.tmp").exists()  # replaced atomically, no temp left


def test_unwritable_heartbeat_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    Heartbeat(blocker / "sub" / "hb.json").beat("gateway")  # parent is a file: swallowed


def test_clear_removes_file(tmp_path: Path) -> None:
    hb = Heartbeat(tmp_path / "hb.json")
    hb.beat("gateway")
    hb.clear()
    assert not (tmp_path / "hb.json").exists()
    hb.clear()  # idempotent


def test_healthcheck_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "hb.json"
    assert healthcheck(p, 60) == 1  # no file
    Heartbeat(p).register("gateway")
    assert healthcheck(p, 60) == 0
    p.write_text(json.dumps({"loops": {"gateway": 1.0}}), encoding="utf-8")  # epoch 1970
    assert healthcheck(p, 60) == 1
    assert main(["healthcheck", "--heartbeat-path", str(p), "--stale-s", "60"]) == 1
    out = capsys.readouterr().out
    assert "unhealthy" in out and "stale" in out


def test_healthcheck_via_python_dash_m_exits_nonzero_on_stale(tmp_path: Path) -> None:
    p = tmp_path / "hb.json"
    p.write_text(json.dumps({"loops": {"gateway": 1.0}}), encoding="utf-8")
    root = Path(__file__).resolve().parents[3]
    cmd = [sys.executable, "-m", "src.app", "healthcheck", "--heartbeat-path", str(p)]
    stale = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=120, check=False)
    assert stale.returncode == 1 and "unhealthy" in stale.stdout
    Heartbeat(p).register("gateway")
    fresh = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=120, check=False)
    assert fresh.returncode == 0
