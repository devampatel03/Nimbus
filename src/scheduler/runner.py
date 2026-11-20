# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Job runner (ADR-0008, P3.3): claim due jobs, run each handler by `kind`,
finish with complete / retry / dead-letter.

* `run_due()` is the `JobRunner` seam the app loops call (`src.app.seams`). It
  claims up to `batch` jobs, runs them concurrently and returns how many ran.
  Catch-up after downtime needs nothing special: every job with `run_at <= now`
  is due, so a restart simply drains the backlog (each job exactly once, because
  the claim is exclusive and the finish is lease-guarded).
* A handler raising `PermanentJobError` dead-letters at once; any other
  exception is retried with backoff and logged by TYPE only (INV-7).
* The handler timeout is shorter than the lease, so a runner that is merely slow
  finishes (or fails) before another worker may steal the job.
* Deferral: a handler raising `JobDeferred(run_at)` puts the job back to pending
  at `run_at` without counting an attempt (model switched off, daily budget used).
* Isolation (R11): build one runner per class of work (`kinds=`): reminders in
  their own runner cannot queue behind a slow `memory_extract`.
* `on_progress` is called after each claim and after each finished job, so a
  loop heartbeat can follow real progress; the handler timeout bounds the gap.
* Cancellation (shutdown) releases the in-flight job untouched; it does not
  burn an attempt and is picked up again on restart.
* A job whose kind has no handler is dead-lettered (never spun on).
* `on_dead[kind]` is a best-effort hook called after the batch has finished, each
  with its own short timeout (`dead_hook_timeout_s`), so a slow alert cannot hold
  other jobs back (for example telling the user a reminder could not be delivered).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.core.clock import Clock
from src.scheduler.jobs import ClaimedJob, JobQueue

log = logging.getLogger(__name__)

DEFAULT_BATCH = 20
DEFAULT_HANDLER_TIMEOUT_S = 120.0  # under the worker heartbeat's staleness limit
DEFAULT_DEAD_HOOK_TIMEOUT_S = 15.0


class PermanentJobError(Exception):
    """Do not retry. The message is stored as the error code, so keep it a fixed
    identifier-like string (no user text)."""


class JobDeferred(Exception):
    """Run again at `run_at` without counting this run as an attempt."""

    def __init__(self, run_at: datetime) -> None:
        super().__init__("deferred")
        self.run_at = run_at


@dataclass(frozen=True)
class JobContext:
    job_id: uuid.UUID
    kind: str
    dedupe_key: str
    payload: dict[str, Any]
    run_at: datetime
    attempt: int
    max_attempts: int
    source_event_id: uuid.UUID | None
    now: datetime  # the runner's clock when the job started


Handler = Callable[[JobContext], Awaitable[None]]


def _ctx(job: ClaimedJob, now: datetime) -> JobContext:
    return JobContext(
        job_id=job.id,
        kind=job.kind,
        dedupe_key=job.dedupe_key,
        payload=job.payload,
        run_at=job.run_at,
        attempt=job.attempts,
        max_attempts=job.max_attempts,
        source_event_id=job.source_event_id,
        now=now,
    )


class JobRunner:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        *,
        clock: Clock,
        kinds: frozenset[str] | None = None,
        batch: int = DEFAULT_BATCH,
        handler_timeout_s: float | None = None,
        on_dead: Mapping[str, Handler] | None = None,
        on_progress: Callable[[], None] | None = None,
        dead_hook_timeout_s: float = DEFAULT_DEAD_HOOK_TIMEOUT_S,
    ) -> None:
        if handler_timeout_s is None:
            handler_timeout_s = min(DEFAULT_HANDLER_TIMEOUT_S, queue.lease_s * 0.8)
        if handler_timeout_s >= queue.lease_s:
            raise ValueError("handler timeout must be shorter than the lease")
        if batch < 1:
            raise ValueError("batch must be positive")
        self._q = queue
        self._handlers = dict(handlers)
        self._clock = clock
        self._kinds = frozenset(self._handlers) if kinds is None else kinds
        self._batch = batch
        self._timeout = handler_timeout_s
        self._on_dead = dict(on_dead or {})
        self._progress = on_progress
        self._hook_timeout = dead_hook_timeout_s
        self._dead: list[ClaimedJob] = []

    def _beat(self) -> None:
        if self._progress is not None:
            self._progress()

    async def run_due(self) -> int:
        claim = await self._q.claim(kinds=self._kinds, limit=self._batch)
        self._beat()
        self._dead = list(claim.dead)
        for expired in claim.dead:
            log.error("job dead-lettered kind=%s reason=LeaseExpired", expired.kind)
        try:
            if claim.jobs:
                await asyncio.gather(*(self._execute(j) for j in claim.jobs))
        finally:
            dead, self._dead = self._dead, []
        if dead:
            await asyncio.gather(*(self._notify_dead(j) for j in dead))
        return len(claim.jobs)

    async def _notify_dead(self, job: ClaimedJob) -> None:
        hook = self._on_dead.get(job.kind)
        if hook is None:
            return
        try:
            await asyncio.wait_for(hook(_ctx(job, self._clock.now())), timeout=self._hook_timeout)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("dead-letter hook failed kind=%s err=%s", job.kind, type(exc).__name__)

    async def _execute(self, job: ClaimedJob) -> None:
        try:
            await self._run_one(job)
        finally:
            self._beat()

    async def _run_one(self, job: ClaimedJob) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            log.error("no handler for job kind=%s; dead-lettering", job.kind)
            await self._finish_failed(job, "NoHandler", permanent=True)
            return
        try:
            await asyncio.wait_for(handler(_ctx(job, self._clock.now())), timeout=self._timeout)
        except asyncio.CancelledError:
            await asyncio.shield(self._release(job))
            raise
        except JobDeferred as exc:
            if not await self._q.defer(job, exc.run_at):
                log.warning("job defer ignored (lease lost) kind=%s", job.kind)
        except PermanentJobError as exc:
            await self._finish_failed(job, str(exc) or "PermanentJobError", permanent=True)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the loop
            await self._finish_failed(job, type(exc).__name__, permanent=False)
        else:
            if not await self._q.complete(job):
                log.warning("job finish ignored (lease lost) kind=%s", job.kind)

    async def _finish_failed(self, job: ClaimedJob, error: str, *, permanent: bool) -> None:
        status = await self._q.fail(job, error, permanent=permanent)
        if status is None:
            log.warning("job failure ignored (lease lost) kind=%s", job.kind)
            return
        log.warning(
            "job failed kind=%s attempt=%d/%d status=%s err=%s",
            job.kind, job.attempts, job.max_attempts, status, error[:60],
        )  # fmt: skip
        if status == "dead":
            self._dead.append(job)  # hook runs after the batch, with its own timeout

    async def _release(self, job: ClaimedJob) -> None:
        try:
            await self._q.release(job)
        except Exception as exc:  # noqa: BLE001 - the lease will expire anyway
            log.warning("job release failed kind=%s err=%s", job.kind, type(exc).__name__)
