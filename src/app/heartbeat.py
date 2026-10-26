# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Loop heartbeats and the `healthcheck` verdict.

Every supervised loop calls `Heartbeat.beat(name)` when it makes progress. The
heartbeat is one small JSON file, replaced atomically (write temp + rename), so
`python -m src.app healthcheck` (a separate, short-lived process, for example a
container HEALTHCHECK) can judge liveness without a database or any app import.

A loop that is crash-looping, wedged, or cut off from the database stops beating
and the file goes stale: the verdict is unhealthy once ANY registered loop has
not beaten for `stale_s`. Beats are only ever issued after real progress (a
completed poll cycle, a finished worker tick), never from the supervisor's
restart path, so restarting does not mask a failure.

The file holds loop names and timestamps only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


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
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:  # never let a heartbeat write kill a loop
            log.warning("heartbeat write failed err=%s", type(exc).__name__)


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    detail: str


def check_heartbeat(path: Path, *, stale_s: float, now: float | None = None) -> HealthReport:
    """Pure verdict over the heartbeat file. Fails closed on anything odd."""
    current = time.time() if now is None else now
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HealthReport(False, "no heartbeat file")
    except (OSError, ValueError):
        return HealthReport(False, "heartbeat file unreadable")
    loops = raw.get("loops") if isinstance(raw, dict) else None
    if not isinstance(loops, dict) or not loops:
        return HealthReport(False, "heartbeat has no loops")
    worst_name = ""
    worst_age = -1.0
    for name, ts in loops.items():
        if not isinstance(name, str) or isinstance(ts, bool) or not isinstance(ts, int | float):
            return HealthReport(False, "heartbeat malformed")
        age = current - float(ts)
        if age > worst_age:
            worst_name, worst_age = name, age
    if worst_age > stale_s:
        return HealthReport(False, f"stale loop={worst_name} age={worst_age:.0f}s")
    return HealthReport(True, f"ok oldest_loop={worst_name} age={max(worst_age, 0):.0f}s")
