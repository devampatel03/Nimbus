# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Assembles the scheduler for the app process (P3.3): one `JobQueue`, the job
handlers by kind, the `JobRunner` the worker loop drives, the `Scheduler` tick,
and the reminder store the tools use. Imports no transport or model SDK: the
gateway, embedder and guarded LLM are injected."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.gateway.protocols import Gateway
from src.llm.protocols import LLMClient
from src.memory.embedder import BatchEmbedder
from src.scheduler import memory_extract as mx
from src.scheduler import reminders as rem
from src.scheduler.jobs import DEFAULT_LEASE_S, JobQueue
from src.scheduler.runner import DEFAULT_BATCH, JobRunner

log = logging.getLogger(__name__)


@dataclass
class SchedulerParts:
    queue: JobQueue
    reminder_runner: JobRunner  # reminder_fire only: never queues behind a model call (R11)
    extract_runner: JobRunner  # memory_extract only: small batch, paced by the caller
    sweeper: mx.MemoryExtractSweeper
    reminders: rem.ReminderStore
    session_factory: async_sessionmaker[AsyncSession]
    extract_handler: mx.MemoryExtractHandler

    async def enqueue_extract_after_turn(self, event_id: uuid.UUID) -> None:
        """Post-turn hook (TARGET_DESIGN 4.1 step 7). Failures are logged by type
        and swallowed: the sweeper backstops a missed enqueue."""
        try:
            async with self.session_factory() as session, session.begin():
                await mx.enqueue_memory_extract(self.queue, session, event_id)
        except Exception as exc:  # noqa: BLE001 - never break the turn loop
            log.warning("memory_extract enqueue failed err=%s", type(exc).__name__)


EXTRACT_CONCURRENCY = 2  # at most two extraction calls in flight (R5)


def build_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    gateway: Gateway,
    embedder: BatchEmbedder,
    llm: LLMClient,
    clock: Clock,
    spend: mx.SpendProbe | None = None,
    lease_s: float = DEFAULT_LEASE_S,
    batch: int = DEFAULT_BATCH,
    on_progress: Callable[[], None] | None = None,
    extract_min_chars: int = mx.MIN_CHARS,
    extract_mode: mx.ExtractMode = "all",
    extract_max_spend_share: float = mx.MAX_SPEND_SHARE,
    reminder_gate: rem.ReminderGate | None = None,
) -> SchedulerParts:
    queue = JobQueue(session_factory, clock, lease_s=lease_s)
    delivery = rem.ReminderDelivery(session_factory, gateway, clock, reminder_gate)
    extract = mx.MemoryExtractHandler(
        session_factory,
        embedder,
        llm,
        clock=clock,
        spend=spend,
        max_spend_share=extract_max_spend_share,
        min_chars=extract_min_chars,
        mode=extract_mode,
    )
    reminder_runner = JobRunner(
        queue,
        {rem.KIND: delivery.handle},
        clock=clock,
        kinds=frozenset({rem.KIND}),
        batch=batch,
        on_dead={rem.KIND: delivery.on_dead},
        on_progress=on_progress,
    )
    extract_runner = JobRunner(
        queue,
        {mx.KIND: extract.handle},
        clock=clock,
        kinds=frozenset({mx.KIND}),
        batch=EXTRACT_CONCURRENCY,
        on_progress=on_progress,
    )
    return SchedulerParts(
        queue=queue,
        reminder_runner=reminder_runner,
        extract_runner=extract_runner,
        sweeper=mx.MemoryExtractSweeper(session_factory, queue, clock, min_chars=extract_min_chars),
        reminders=rem.ReminderStore(session_factory, queue, clock),
        session_factory=session_factory,
        extract_handler=extract,
    )
