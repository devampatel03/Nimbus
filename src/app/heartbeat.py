# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Loop heartbeats and the `healthcheck` verdict.

Every supervised loop calls `Heartbeat.beat(name)` when it makes progress. The
heartbeat is one small JSON file, replaced atomically (write temp + rename), so
`python -m src.app healthcheck` (a separate, short-lived process, for example a
container HEALTHCHECK) can judge liveness without a database or any app import.

What counts as progress (a beat is never issued from a timer that runs
independently of the work it vouches for):

* gateway: a completed `getUpdates` cycle (an empty long poll counts) or a
  finished turn. A hung poll, a database that stops answering, or a wedged
  ingest stops the beats.
* worker / scheduler: the loop's own iterations. A loop that is crash-looping
  never lives long enough to beat, and restarting never beats on its behalf.

The verdict is unhealthy when the file is missing or malformed, the loop set is
not exactly `LOOP_NAMES`, any timestamp is non-finite or more than
`MAX_FUTURE_S` in the future (a forged or skewed file must not read as healthy),
any loop is older than `stale_s`, or the file lives somewhere other users could
tamper with it (world-writable directory, or not owned by us; POSIX only).

The file holds loop names and timestamps only.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

LOOP_NAMES: tuple[str, ...] = ("gateway", "worker", "scheduler")
MAX_FUTURE_S = 60.0  # clock skew allowance; beyond it a heartbeat is forged/broken


class HeartbeatPathError(RuntimeError):
    """The heartbeat location is not private to this user (fixed message)."""


def location_problem(*, dir_mode: int, dir_uid: int, file_uid: int | None, euid: int) -> str | None:
    """Pure policy for where the heartbeat may live. A world-writable directory
    (for example /tmp) or one owned by someone else lets another local user
    forge or delete the file; a file we do not own is the same problem."""
    if dir_mode & stat.S_IWOTH:
        return "heartbeat directory is world-writable"
    if dir_uid != euid:
        return "heartbeat directory is not owned by this user"
    if file_uid is not None and file_uid != euid:
        return "heartbeat file is not owned by this user"
    return None


def _check_location(path: Path) -> str | None:
    if sys.platform == "win32":  # no POSIX modes; the per-user LOCALAPPDATA default applies
        return None
    try:
        d = os.stat(path.parent)
        f = os.lstat(path) if path.exists() or path.is_symlink() else None
    except OSError:
        return "heartbeat location unreadable"
    return location_problem(
        dir_mode=d.st_mode,
        dir_uid=d.st_uid,
        file_uid=None if f is None else f.st_uid,
        euid=os.geteuid(),
    )


class Heartbeat:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        pid: int | None = None,
    ) -> None:
        self._path = path
        self._clock = clock
        self._pid = os.getpid() if pid is None else pid
        self._started = clock()
        self._loops: dict[str, float] = {}

    def prepare(self) -> None:
        """Create the directory private (0700) and refuse an unsafe location.
        Raises `HeartbeatPathError`; call once at startup."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise HeartbeatPathError("heartbeat directory cannot be created") from None
        problem = _check_location(self._path)
        if problem is not None:
            raise HeartbeatPathError(problem)

    def register(self, *names: str) -> None:
        """Count the loops from the start: a loop that never beats is stale."""
        now = self._clock()
        for name in names:
            self._loops.setdefault(name, now)
        self._flush()

    def beat(self, name: str) -> None:
        self._loops[name] = self._clock()
        self._flush()

    def clear(self) -> None:
        """Clean shutdown: no heartbeat means not healthy."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("heartbeat clear failed err=%s", type(exc).__name__)

    def _flush(self) -> None:
        body = json.dumps({"pid": self._pid, "started": self._started, "loops": self._loops})
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, self._path)
        except OSError as exc:  # never let a heartbeat write kill a loop
            log.warning("heartbeat write failed err=%s", type(exc).__name__)


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    detail: str


def check_heartbeat(
    path: Path,
    *,
    stale_s: float,
    now: float | None = None,
    required: tuple[str, ...] = LOOP_NAMES,
) -> HealthReport:
    """Pure verdict over the heartbeat file. Fails closed on anything odd."""
    current = time.time() if now is None else now
    if not math.isfinite(stale_s) or not math.isfinite(current):
        return HealthReport(False, "invalid threshold or clock")
    problem = _check_location(path)
    if problem is not None:
        return HealthReport(False, problem)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HealthReport(False, "no heartbeat file")
    except (OSError, ValueError):
        return HealthReport(False, "heartbeat file unreadable")
    loops = raw.get("loops") if isinstance(raw, dict) else None
    if not isinstance(loops, dict) or not loops:
        return HealthReport(False, "heartbeat has no loops")
    if set(loops) != set(required):
        return HealthReport(False, "heartbeat loop set is wrong")
    worst_name = ""
    worst_age = -math.inf
    for name, ts in loops.items():
        try:
            valid = not isinstance(ts, bool) and isinstance(ts, int | float) and math.isfinite(ts)
            age = current - float(ts) if valid else 0.0
        except OverflowError:  # a 400-digit JSON integer
            valid = False
        if not valid:
            return HealthReport(False, "heartbeat malformed")
        if age < -MAX_FUTURE_S:
            return HealthReport(False, f"future-dated heartbeat loop={name}")
        if age > worst_age:
            worst_name, worst_age = name, age
    if worst_age > stale_s:
        return HealthReport(False, f"stale loop={worst_name} age={worst_age:.0f}s")
    return HealthReport(True, f"ok oldest_loop={worst_name} age={max(worst_age, 0):.0f}s")
