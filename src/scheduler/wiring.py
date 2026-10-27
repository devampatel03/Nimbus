# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Assembles the scheduler for the app process (P3.3): one `JobQueue`, the job
handlers by kind, the `JobRunner` the worker loop drives, the `Scheduler` tick,
and the reminder store the tools use. Imports no transport or model SDK: the
gateway, embedder and guarded LLM are injected."""

from __future__ import annotations

import logging
import uuid
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
    runner: JobRunner
    sweeper: mx.MemoryExtractSweeper
    reminders: rem.ReminderStore
    session_factory: async_sessionmaker[AsyncSession]

    async def enqueue_extract_after_turn(self, event_id: uuid.UUID) -> None:
        """Post-turn hook (TARGET_DESIGN 4.1 step 7). Failures are logged by type
        and swallowed: the sweeper backstops a missed enqueue."""
        try:
            async with self.session_factory() as session, session.begin():
                await mx.enqueue_memory_extract(self.queue, session, event_id)
        except Exception as exc:  # noqa: BLE001 - never break the turn loop
            log.warning("memory_extract enqueue failed err=%s", type(exc).__name__)


def build_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    gateway: Gateway,
    embedder: BatchEmbedder,
    llm: LLMClient,
    clock: Clock,
    lease_s: float = DEFAULT_LEASE_S,
    batch: int = DEFAULT_BATCH,
) -> SchedulerParts:
    queue = JobQueue(session_factory, clock, lease_s=lease_s)
    delivery = rem.ReminderDelivery(session_factory, gateway, clock)
    extract = mx.MemoryExtractHandler(session_factory, embedder, llm)
    runner = JobRunner(
        queue,
        {rem.KIND: delivery.handle, mx.KIND: extract.handle},
        clock=clock,
        batch=batch,
        on_dead={rem.KIND: delivery.on_dead},
    )
    return SchedulerParts(
        queue=queue,
        runner=runner,
        sweeper=mx.MemoryExtractSweeper(session_factory, queue, clock),
        reminders=rem.ReminderStore(session_factory, queue, clock),
        session_factory=session_factory,
    )
