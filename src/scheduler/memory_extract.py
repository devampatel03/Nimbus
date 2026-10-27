# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The memory write path (P3.3; TARGET_DESIGN 4.1 step 7; closes W2 gate MED-1).

After a TRUSTED user event, a `memory_extract` job is enqueued
(`enqueue_memory_extract`, deduped on the event id). The handler:

1. loads the stored event; refuses anything that is not a trusted user text or
   voice transcript (forwards, via_bot, Recorder transcripts and any third-party
   speech are `lane='untrusted'`: SKIPPED with a log line, never extracted into
   trusted memory here; the quarantine lane is P3.0). Nothing is promoted: the
   store gives a chunk its event's lane and `promoted = lane == 'trusted'`.
2. writes the event text itself as chunk 0 (secrets redacted first, INV-7)
   through `MemoryManager.write_episodic` against the REAL `event_id`.
3. extracts relations, commitments and decisions through the guarded LLM
   (`src.llm.protocols.LLMClient`, purpose MEMORY_EXTRACT, trusted lane) and writes
   one short chunk per item as chunks 1..n.

Idempotent on `(event_id, chunk_index)`: a replay neither re-embeds nor rewrites.
If extracted chunks already exist the model is not called again (no second
spend). The event chunk is committed BEFORE the model call, so a provider
outage never loses the user's own words; the retry only redoes the extraction.

Failure classes: a guard `scrub_blocked`, an unusable model answer, a missing
event or a bad payload will never succeed on retry -> `PermanentJobError`
(dead letter). Provider errors, kill switch and spend cap are transient ->
the exception propagates and the runner backs off.

Graph (`graph_edge`) and `commitment` rows are not written here: they need a
legacy `session` row (source_session FK) that the Telegram path does not have.
Their rewrite onto `event` is a recorded follow-up.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import String, cast, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.contracts.transcript import Segment, Transcript
from src.core.clock import Clock
from src.llm.protocols import LLMClient
from src.llm.scrub import redact_secrets
from src.llm.types import LLMRefused, LLMResponseError
from src.memory.embedder import BatchEmbedder
from src.memory.extractor import (
    CommitmentExtractor,
    DecisionExtractor,
    ExtractedCommitment,
    ExtractedDecision,
    ExtractedRelation,
    RelationExtractor,
)
from src.memory.manager import MemoryManager
from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.memory_chunk import MemoryChunk
from src.memory.schemas.ops import Job
from src.memory.store import ChunkIn
from src.scheduler.jobs import JobQueue
from src.scheduler.runner import JobContext, PermanentJobError

log = logging.getLogger(__name__)

KIND = "memory_extract"
EXTRACTABLE_KINDS = frozenset({"text", "voice_transcript"})
MIN_CHARS = 8
MAX_CHUNK_CHARS = 2000
MAX_EXTRACTED_CHUNKS = 20


def dedupe_key(event_id: uuid.UUID) -> str:
    return f"{KIND}:{event_id}"


async def enqueue_memory_extract(
    queue: JobQueue, session: AsyncSession, event_id: uuid.UUID, *, run_at: datetime | None = None
) -> bool:
    """Enqueue in the caller's transaction. True if newly created."""
    _, created = await queue.enqueue_in(
        session,
        KIND,
        dedupe_key=dedupe_key(event_id),
        payload={"event_id": str(event_id)},
        run_at=run_at,
        source_event_id=event_id,
    )
    return created


def _clip(text: str, limit: int = MAX_CHUNK_CHARS) -> str:
    return redact_secrets(" ".join(text.split()))[:limit]


def _relation_line(r: ExtractedRelation) -> str:
    obj = r.object_literal if r.object_literal is not None else (r.object_name or "")
    return _clip(f"{r.subject_name} {r.predicate} {obj}")


def _commitment_line(c: ExtractedCommitment) -> str:
    due = f" (due {c.due_at.date().isoformat()})" if c.due_at is not None else ""
    return _clip(f"Commitment: {c.description}{due}")


def _decision_line(d: ExtractedDecision) -> str:
    return _clip(f"Decision: {d.decision_text} (why: {d.reasoning})")


class MemoryExtractHandler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: BatchEmbedder,
        llm: LLMClient,
    ) -> None:
        self._sf = session_factory
        self._embedder = embedder
        self._llm = llm

    @staticmethod
    def _event_id(ctx: JobContext) -> uuid.UUID:
        try:
            return uuid.UUID(str(ctx.payload["event_id"]))
        except (KeyError, ValueError):
            raise PermanentJobError("bad_payload") from None

    async def _write(self, event_id: uuid.UUID, chunks: list[ChunkIn]) -> None:
        async with self._sf() as session, session.begin():
            mgr = MemoryManager(pg_session=session, embedder=self._embedder)
            await mgr.write_episodic(event_id, chunks)

    async def handle(self, ctx: JobContext) -> None:
        event_id = self._event_id(ctx)
        async with self._sf() as session:
            ev = (
                await session.execute(
                    select(EventRow.lane, EventRow.kind, EventRow.body, EventRow.occurred_at).where(
                        EventRow.id == event_id
                    )
                )
            ).one_or_none()
            extracted = await session.scalar(
                select(func.count())
                .select_from(MemoryChunk)
                .where(MemoryChunk.source_event_id == event_id, MemoryChunk.chunk_index >= 1)
            )
        if ev is None:
            raise PermanentJobError("event_missing")
        if ev.lane != "trusted":
            # INV-6 / R8: untrusted text is never extracted into trusted memory here.
            log.info("memory_extract skipped: untrusted event kind=%s", ev.kind)
            return
        if ev.kind not in EXTRACTABLE_KINDS:
            log.info("memory_extract skipped: unsupported event kind=%s", ev.kind)
            return
        body = (ev.body or "").strip()
        if len(body) < MIN_CHARS:
            log.info("memory_extract skipped: too short")
            return

        await self._write(event_id, [ChunkIn(text=_clip(body), chunk_index=0, importance=0.5)])
        if extracted:
            return  # an earlier run finished the extraction: no second model spend

        transcript = Transcript(
            segments=[Segment(text=body)],
            source_model="user_text",
            untrusted=False,
            created_at=ev.occurred_at,
        )
        items: list[ChunkIn] = []
        try:
            for rel in await RelationExtractor(self._llm).extract(
                transcript, source_event_id=event_id
            ):
                items.append(
                    ChunkIn(text=_relation_line(rel), importance=0.6, confidence=rel.confidence)
                )
            for com in await CommitmentExtractor(self._llm).extract(
                transcript, source_event_id=event_id
            ):
                items.append(
                    ChunkIn(text=_commitment_line(com), importance=0.8, confidence=com.confidence)
                )
            for dec in await DecisionExtractor(self._llm).extract(
                transcript, source_event_id=event_id
            ):
                items.append(
                    ChunkIn(text=_decision_line(dec), importance=0.8, confidence=dec.confidence)
                )
        except LLMRefused as exc:
            if exc.reason == "scrub_blocked":
                raise PermanentJobError("scrub_blocked") from None
            raise  # kill switch / spend cap / guard error: transient, retried with backoff
        except LLMResponseError:
            raise PermanentJobError("bad_response") from None

        numbered = [
            ChunkIn(
                text=c.text,
                chunk_index=i,
                importance=c.importance,
                confidence=c.confidence,
            )
            for i, c in enumerate(
                [c for c in items if c.text.strip()][:MAX_EXTRACTED_CHUNKS], start=1
            )
        ]
        if numbered:
            await self._write(event_id, numbered)
        log.info("memory_extract done chunks=%d", 1 + len(numbered))


class MemoryExtractSweeper:
    """The `Scheduler` seam (`tick()`): a backstop for the post-turn enqueue. It
    enqueues `memory_extract` for every trusted user text / voice-transcript
    event of the last `lookback` that has no job yet (a crash between the turn and
    the enqueue, and voice transcripts, which start no turn). Untrusted events
    are never selected. Idempotent: the job's dedupe key is the event id."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: JobQueue,
        clock: Clock,
        *,
        lookback: timedelta = timedelta(days=2),
        limit: int = 50,
    ) -> None:
        self._sf = session_factory
        self._q = queue
        self._clock = clock
        self._lookback = lookback
        self._limit = limit

    async def tick(self) -> None:
        since = self._clock.now() - self._lookback
        async with self._sf() as session, session.begin():
            missing = await session.scalars(
                select(EventRow.id)
                .where(
                    EventRow.lane == "trusted",
                    EventRow.kind.in_(sorted(EXTRACTABLE_KINDS)),
                    EventRow.created_at >= since,
                    func.char_length(func.coalesce(EventRow.body, "")) >= MIN_CHARS,
                    ~exists().where(
                        Job.dedupe_key == literal(f"{KIND}:") + cast(EventRow.id, String)
                    ),
                )
                .order_by(EventRow.created_at)
                .limit(self._limit)
            )
            for event_id in list(missing):
                await enqueue_memory_extract(self._q, session, event_id)
