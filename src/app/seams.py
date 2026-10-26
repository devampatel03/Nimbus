# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Seams for P3.3 (scheduler + Postgres `SKIP LOCKED` job runner, ADR-0008).

Neither exists yet. The app loops call these Protocols so P3.3 plugs in an
implementation without touching the entrypoint; until then the no-ops run (and
beat the heartbeat), so supervision and health reporting are already real.
"""

from __future__ import annotations

from typing import Protocol


class JobRunner(Protocol):
    async def run_due(self) -> int:
        """Claim and run due jobs; returns how many ran."""
        ...


class Scheduler(Protocol):
    async def tick(self) -> None:
        """Enqueue whatever is due now (reminders, catch-up after downtime)."""
        ...


class NoopJobRunner:
    async def run_due(self) -> int:
        return 0


class NoopScheduler:
    async def tick(self) -> None:
        return None
