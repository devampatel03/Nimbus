# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

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
* Cancellation (shutdown) releases the in-flight job untouched; it does not
  burn an attempt and is picked up again on restart.
* A job whose kind has no handler is dead-lettered (never spun on).
* `on_dead[kind]` is a best-effort hook called after a job is dead-lettered (for
  example to tell the user a reminder could not be delivered).
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
DEFAULT_HANDLER_TIMEOUT_S = 240.0


class PermanentJobError(Exception):
    """Do not retry. The message is stored as the error code, so keep it a fixed
    identifier-like string (no user text)."""


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

    async def run_due(self) -> int:
        claim = await self._q.claim(kinds=self._kinds, limit=self._batch)
        for dead in claim.dead:
            log.error("job dead-lettered kind=%s reason=LeaseExpired", dead.kind)
            await self._notify_dead(dead)
        if not claim.jobs:
            return 0
        await asyncio.gather(*(self._execute(j) for j in claim.jobs))
        return len(claim.jobs)

    async def _notify_dead(self, job: ClaimedJob) -> None:
        hook = self._on_dead.get(job.kind)
        if hook is None:
            return
        try:
            await asyncio.wait_for(hook(_ctx(job, self._clock.now())), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("dead-letter hook failed kind=%s err=%s", job.kind, type(exc).__name__)

    async def _execute(self, job: ClaimedJob) -> None:
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
            await self._notify_dead(job)

    async def _release(self, job: ClaimedJob) -> None:
        try:
            await self._q.release(job)
        except Exception as exc:  # noqa: BLE001 - the lease will expire anyway
            log.warning("job release failed kind=%s err=%s", job.kind, type(exc).__name__)
