# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Heartbeat file + healthcheck verdict (fail closed on anything odd)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.app.cli import healthcheck, main
from src.app.heartbeat import (
    LOOP_NAMES,
    Heartbeat,
    HeartbeatPathError,
    check_heartbeat,
    location_problem,
)
from src.app.settings import AppSettings


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    clock = Clock()
    hb = Heartbeat(tmp_path / "hb.json", clock=clock)
    hb.register(*LOOP_NAMES)
    clock.t += 10
    for name in LOOP_NAMES:
        hb.beat(name)
    assert check_heartbeat(tmp_path / "hb.json", stale_s=60, now=clock.t + 5).ok


def test_one_stale_loop_makes_the_process_unhealthy(tmp_path: Path) -> None:
    clock = Clock()
    hb = Heartbeat(tmp_path / "hb.json", clock=clock)
    hb.register(*LOOP_NAMES)
    clock.t += 100
    for name in ("gateway", "scheduler"):
        hb.beat(name)  # worker never beats again
    rep = check_heartbeat(tmp_path / "hb.json", stale_s=60, now=clock.t + 1)
    assert not rep.ok and "worker" in rep.detail


def test_registered_loop_that_never_beats_goes_stale(tmp_path: Path) -> None:
    clock = Clock()
    Heartbeat(tmp_path / "hb.json", clock=clock).register(*LOOP_NAMES)
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
        '{"loops": {"gateway": 1}}',  # incomplete loop set
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
    hb.register(*LOOP_NAMES)
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "pid": 7,
        "started": 1000.0,
        "loops": dict.fromkeys(LOOP_NAMES, 1000.0),
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
    Heartbeat(p).register(*LOOP_NAMES)
    assert healthcheck(p, 60) == 0
    p.write_text(json.dumps({"loops": dict.fromkeys(LOOP_NAMES, 1.0)}), encoding="utf-8")  # 1970
    assert healthcheck(p, 60) == 1
    assert main(["healthcheck", "--heartbeat-path", str(p), "--stale-s", "60"]) == 1
    out = capsys.readouterr().out
    assert "unhealthy" in out and "stale" in out


def test_healthcheck_via_python_dash_m_exits_nonzero_on_stale(tmp_path: Path) -> None:
    p = tmp_path / "hb.json"
    p.write_text(json.dumps({"loops": dict.fromkeys(LOOP_NAMES, 1.0)}), encoding="utf-8")
    root = Path(__file__).resolve().parents[3]
    cmd = [sys.executable, "-m", "src.app", "healthcheck", "--heartbeat-path", str(p)]
    stale = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=120, check=False)
    assert stale.returncode == 1 and "unhealthy" in stale.stdout
    Heartbeat(p).register(*LOOP_NAMES)
    fresh = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=120, check=False)
    assert fresh.returncode == 0


def _write(p: Path, loops: dict[str, object]) -> None:
    p.write_text(json.dumps({"pid": 1, "started": 0, "loops": loops}), encoding="utf-8")


def _loops(ts: object) -> dict[str, object]:
    return dict.fromkeys(LOOP_NAMES, ts)


@pytest.mark.parametrize("ts", ["Infinity", "-Infinity", "NaN", "1e309", "-1e309", "9" * 400])
def test_non_finite_timestamps_are_malformed_not_healthy(tmp_path: Path, ts: str) -> None:
    """MED-1: json accepts Infinity/NaN/1e309; age arithmetic on them must not pass."""
    p = tmp_path / "hb.json"
    names = ", ".join(f'"{n}": {ts}' for n in LOOP_NAMES)
    p.write_text(f'{{"loops": {{{names}}}}}', encoding="utf-8")
    rep = check_heartbeat(p, stale_s=60, now=1000.0)
    assert not rep.ok and "malformed" in rep.detail


def test_non_finite_now_or_one_bad_loop_among_good_ones_is_unhealthy(tmp_path: Path) -> None:
    p = tmp_path / "hb.json"
    p.write_text(
        '{"loops": {"gateway": 1000, "worker": 1000, "scheduler": Infinity}}', encoding="utf-8"
    )
    assert not check_heartbeat(p, stale_s=60, now=1001.0).ok


def test_future_dated_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    p = tmp_path / "hb.json"
    _write(p, _loops(10_000.0))
    rep = check_heartbeat(p, stale_s=60, now=1000.0)
    assert not rep.ok and "future" in rep.detail
    _write(p, _loops(1050.0))  # within the 60 s skew allowance
    assert check_heartbeat(p, stale_s=60, now=1000.0).ok


def test_loop_set_must_be_exactly_the_three_loops(tmp_path: Path) -> None:
    p = tmp_path / "hb.json"
    _write(p, {"gateway": 1000.0, "worker": 1000.0})  # scheduler missing
    assert not check_heartbeat(p, stale_s=60, now=1001.0).ok
    _write(p, {**_loops(1000.0), "extra": 1000.0})  # unknown extra loop
    assert not check_heartbeat(p, stale_s=60, now=1001.0).ok
    _write(p, _loops(1000.0))
    assert check_heartbeat(p, stale_s=60, now=1001.0).ok


def test_non_finite_stale_threshold_fails_closed(tmp_path: Path) -> None:
    p = tmp_path / "hb.json"
    _write(p, _loops(1.0))
    assert not check_heartbeat(p, stale_s=float("inf"), now=1e9).ok
    assert not check_heartbeat(p, stale_s=float("nan"), now=1e9).ok


def test_healthcheck_cli_rejects_future_and_infinite_via_exit_code(tmp_path: Path) -> None:
    p = tmp_path / "hb.json"
    p.write_text('{"loops": {"gateway": 1e309, "worker": 1e309, "scheduler": 1e309}}')
    assert healthcheck(p, 60) == 1
    _write(p, _loops(time.time() + 86_400))
    assert healthcheck(p, 60) == 1


# ---- location policy (MED-1: never /tmp, owner-only) ----

ME = 1000


def test_location_policy_is_pure_and_strict() -> None:
    ok = {"dir_mode": 0o40700, "dir_uid": ME, "file_uid": ME, "euid": ME}
    assert location_problem(**ok) is None
    assert location_problem(**{**ok, "file_uid": None}) is None  # file not created yet
    assert "world-writable" in (location_problem(**{**ok, "dir_mode": 0o41777}) or "")  # /tmp
    assert "world-writable" in (location_problem(**{**ok, "dir_mode": 0o40702}) or "")
    assert "directory is not owned" in (location_problem(**{**ok, "dir_uid": 0}) or "")
    assert "file is not owned" in (location_problem(**{**ok, "file_uid": 0}) or "")


def test_default_heartbeat_path_is_not_under_the_shared_temp_dir() -> None:
    import tempfile

    path = AppSettings(_env_file=None).heartbeat_path
    assert Path(tempfile.gettempdir()) not in path.parents
    assert "nimbus" in str(path).lower() and path.is_absolute()


def test_default_path_honours_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.app.settings import default_heartbeat_path

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_heartbeat_path().is_relative_to(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_prepare_creates_a_private_directory_and_refuses_a_shared_one(tmp_path: Path) -> None:
    import os
    import stat

    hb = Heartbeat(tmp_path / "state" / "nimbus" / "hb.json")
    hb.prepare()
    assert stat.S_IMODE(os.stat(tmp_path / "state" / "nimbus").st_mode) == 0o700
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(HeartbeatPathError, match="world-writable"):
        Heartbeat(shared / "hb.json").prepare()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_healthcheck_refuses_a_heartbeat_in_a_world_writable_dir(tmp_path: Path) -> None:
    d = tmp_path / "pub"
    d.mkdir()
    p = d / "hb.json"
    Heartbeat(p).register(*LOOP_NAMES)
    assert check_heartbeat(p, stale_s=60).ok
    d.chmod(0o777)
    rep = check_heartbeat(p, stale_s=60)
    assert not rep.ok and "world-writable" in rep.detail


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_heartbeat_file_is_owner_only(tmp_path: Path) -> None:
    import os
    import stat

    p = tmp_path / "hb.json"
    Heartbeat(p).beat("gateway")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
