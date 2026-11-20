# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""The memory write path (P3.3; TARGET_DESIGN 4.1 step 7; closes W2 gate MED-1).

After a TRUSTED user event, a `memory_extract` job is enqueued
(`enqueue_memory_extract`, deduped on the event id). The handler:

1. loads the stored event; refuses anything that is not a trusted user text or
   voice transcript (forwards, via_bot, Recorder transcripts and any third-party
   speech are `lane='untrusted'`: SKIPPED with a log line, never extracted into
   trusted memory here; the quarantine lane is P3.0). Nothing is promoted: the
   store gives a chunk its event's lane and `promoted = lane == 'trusted'`.
2. skips what is not worth a model call: under `min_chars` (default
   `MIN_CHARS`), or only an acknowledgement / greeting (deterministic word list,
   no model). In `mode="cue"` (P5.5 cost lever) it also skips any message with
   neither a commitment cue nor a first-person memory cue (`has_memory_cue`).
3. defers (never fails, never burns an attempt) while the model is off
   (`llm.mode() != NORMAL`: kill switch or daily cap) or today's spend is above
   `max_spend_share` of the cap. The job comes back after `RECHECK`.
4. writes the event text itself as chunk 0 (secrets redacted first, INV-7)
   through `MemoryManager.write_episodic` against the REAL `event_id`.
5. extracts relations, commitments and decisions with ONE combined guarded call
   (`CombinedExtractor`, purpose MEMORY_EXTRACT, trusted lane, output capped at
   300 tokens) and writes one short chunk per item as chunks 1..n.

Idempotent on `(event_id, chunk_index)`: a replay neither re-embeds nor rewrites.
If extracted chunks already exist the model is not called again (no second
spend). The event chunk is committed BEFORE the model call, so a provider
outage never loses the user's own words; the retry only redoes the extraction.

Failure classes: a guard `scrub_blocked`, an unusable model answer, a missing
event or a bad payload will never succeed on retry -> `PermanentJobError`
(dead letter). `kill_switch` / `spend_cap` / `rate_limited` (the request
limiter, or a provider 429: background yields to chat, P5.5) -> `JobDeferred`. Provider errors and
other guard errors are transient -> the exception propagates and the runner
backs off.

The sweeper (`MemoryExtractSweeper`, the `Scheduler` tick) backstops the post-turn
enqueue: trusted events with no live job get one, at most `MAX_ENQUEUE_PER_MIN`
a minute (catch-up after downtime must not be a spending spike, R5). A job that
died of a transient cause is enqueued again after a cool-down; one that died
permanently is not.

Graph (`graph_edge`) and `commitment` rows are not written here: they need a
legacy `session` row (source_session FK) that the Telegram path does not have.
Their rewrite onto `event` is a recorded follow-up.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel
from sqlalchemy import Integer, String, and_, cast, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.contracts.transcript import Segment, Transcript
from src.core.clock import Clock
from src.llm.guard import ist_day_bounds
from src.llm.protocols import LLMClient, SpendLedger
from src.llm.scrub import redact_secrets
from src.llm.types import LLMMode, LLMProviderError, LLMRefused, LLMResponseError
from src.memory.commitments import CommitmentIntake, has_commitment_cue
from src.memory.embedder import BatchEmbedder
from src.memory.extractor import (
    CombinedExtractor,
    ExtractedCommitment,
    ExtractedDecision,
    ExtractedRelation,
)
from src.memory.manager import MemoryManager
from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.memory_chunk import MemoryChunk
from src.memory.schemas.ops import Job
from src.memory.store import ChunkIn
from src.scheduler.jobs import JobQueue
from src.scheduler.runner import JobContext, JobDeferred, PermanentJobError

log = logging.getLogger(__name__)

KIND = "memory_extract"
EXTRACTABLE_KINDS = frozenset({"text", "voice_transcript"})
MIN_CHARS = 40
MAX_CHUNK_CHARS = 2000
MAX_EXTRACTED_CHUNKS = 20
RECHECK = timedelta(minutes=30)  # how long a deferred job waits before looking again
MAX_SPEND_SHARE = 0.30  # extraction pauses once the day's spend passes this share of the cap
MAX_ENQUEUE_PER_MIN = 10
SWEEP_LOOKBACK = timedelta(days=2)
RETRY_DEAD_AFTER = timedelta(hours=1)
MAX_REVIVALS = 3  # the sweeper revives a transiently-dead job at most this often
PURGE_AFTER = timedelta(days=14)  # finished jobs older than this are deleted (> SWEEP_LOOKBACK)
PURGE_EVERY = timedelta(hours=1)
# Dead for these reasons will not get better by itself: never re-enqueued.
PERMANENT_ERRORS = ("scrub_blocked", "bad_response", "event_missing", "bad_payload", "NoHandler")

_ACK_WORDS = frozenset(
    [
        "a",
        "all",
        "and",
        "appreciate",
        "awesome",
        "bye",
        "cheers",
        "cool",
        "fine",
        "good",
        "great",
        "hello",
        "hey",
        "hi",
        "hmm",
        "ok",
        "okay",
        "nice",
        "no",
        "noted",
        "now",
        "of",
        "ok",
        "oh",
        "perfect",
        "please",
        "right",
        "sounds",
        "sure",
        "thank",
        "thanks",
        "that",
        "the",
        "this",
        "to",
        "too",
        "very",
        "welcome",
        "well",
        "yeah",
        "yep",
        "yes",
        "you",
        "so",
        "much",
        "it",
        "got",
        "morning",
        "evening",
        "afternoon",
        "night",
        "hope",
        "are",
        "doing",
        "lol",
        "haha",
        "thx",
        "ty",
        "np",
        "alright",
        "done",
        "will",
        "do",
        "me",
        "my",
        "is",
        "be",
        "see",
        "later",
        "again",
        "take",
        "care",
        "have",
        "day",
        "buddy",
        "mate",
        "man",
        "bro",
    ]
)
_WORD_RE = re.compile(r"[a-z']+", re.ASCII)
# First-person facts, preferences, plans and decisions: worth remembering.
_MEMORY_CUE_RE = re.compile(
    r"\b(?:i|i'm|im|i've|ive|i'll|ill|i'd|my|mine|me|we|our|remember|decided|decide|"
    r"prefer|favou?rite|birthday|anniversary|allergic|always|never|usually|"
    r"moving|moved|started|starting|joined|quit|plan|planning|going to)\b",
    re.IGNORECASE,
)
ExtractMode = Literal["all", "cue"]


def has_memory_cue(text: str) -> bool:
    """Deterministic cue for `mode="cue"`: a first-person or memory word."""
    return bool(_MEMORY_CUE_RE.search(text))


def is_low_signal(text: str) -> bool:
    """True for acknowledgements and greetings (deterministic, no model): every word
    is a stock pleasantry. Anything with a content word goes to extraction."""
    words = _WORD_RE.findall(text.lower())
    return not words or all(w in _ACK_WORDS for w in words)


def dedupe_key(event_id: uuid.UUID) -> str:
    return f"{KIND}:{event_id}"


async def enqueue_memory_extract(
    queue: JobQueue,
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    run_at: datetime | None = None,
    rearm: bool = False,
    revivals: int = 0,
) -> bool:
    """Enqueue in the caller's transaction. True if newly created. `rearm` also
    revives a finished (e.g. dead) job under the same key."""
    _, created = await queue.enqueue_in(
        session,
        KIND,
        dedupe_key=dedupe_key(event_id),
        payload={"event_id": str(event_id), "revivals": revivals},
        run_at=run_at,
        source_event_id=event_id,
        max_attempts=8,
        rearm=rearm,
    )
    return created


class SpendProbe(Protocol):
    async def fraction_used(self) -> float:
        """Today's (IST day) LLM spend as a fraction of the daily cap."""
        ...


class LedgerSpendProbe:
    def __init__(self, ledger: SpendLedger, cap_inr: Decimal, clock: Clock) -> None:
        self._ledger = ledger
        self._cap = cap_inr
        self._clock = clock

    async def fraction_used(self) -> float:
        _, start, end = ist_day_bounds(self._clock.now())
        spent = await self._ledger.spent_between(start, end)
        return float(spent / self._cap) if self._cap > 0 else 1.0


def _clip(text: str, limit: int = MAX_CHUNK_CHARS) -> str:
    return redact_secrets(" ".join(text.split()))[:limit]


def _line(item: BaseModel) -> str | None:
    if isinstance(item, ExtractedRelation):
        obj = item.object_literal if item.object_literal is not None else (item.object_name or "")
        return _clip(f"{item.subject_name} {item.predicate} {obj}")
    if isinstance(item, ExtractedCommitment):
        due = f" ({item.due_phrase})" if item.due_phrase else ""
        return _clip(f"Commitment: {item.description}{due}")
    if isinstance(item, ExtractedDecision):
        return _clip(f"Decision: {item.decision_text} (why: {item.reasoning})")
    return None


def _importance(item: BaseModel) -> float:
    return 0.6 if isinstance(item, ExtractedRelation) else 0.8


class MemoryExtractHandler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: BatchEmbedder,
        llm: LLMClient,
        *,
        clock: Clock,
        spend: SpendProbe | None = None,
        max_spend_share: float = MAX_SPEND_SHARE,
        commitments: CommitmentIntake | None = None,
        min_chars: int = MIN_CHARS,
        mode: ExtractMode = "all",
    ) -> None:
        if min_chars < 1 or mode not in ("all", "cue"):
            raise ValueError("bad extraction settings")
        self._min_chars = min_chars
        self._mode = mode
        self._commitments = commitments
        self._sf = session_factory
        self._embedder = embedder
        self._llm = llm
        self._clock = clock
        self._spend = spend
        self._share = max_spend_share

    def bind_commitments(self, intake: CommitmentIntake) -> None:
        """P3.9 seam: candidate commitments from the combined extraction."""
        self._commitments = intake

    @staticmethod
    def _event_id(ctx: JobContext) -> uuid.UUID:
        try:
            return uuid.UUID(str(ctx.payload["event_id"]))
        except (KeyError, ValueError):
            raise PermanentJobError("bad_payload") from None

    def _defer(self, why: str) -> JobDeferred:
        log.info("memory_extract deferred reason=%s", why)
        return JobDeferred(self._clock.now() + RECHECK)

    async def _write(self, event_id: uuid.UUID, chunks: list[ChunkIn]) -> None:
        async with self._sf() as session, session.begin():
            mgr = MemoryManager(pg_session=session, embedder=self._embedder)
            await mgr.write_episodic(event_id, chunks)

    async def _record_commitments(self, event_id: uuid.UUID, items: list[BaseModel]) -> None:
        """Candidates only (P3.9): the user confirms by card; never auto-confirmed."""
        if self._commitments is None:
            return
        found = [
            (i.description, i.confidence, i.due_phrase)
            for i in items
            if isinstance(i, ExtractedCommitment)
        ]
        if not found:
            return
        try:
            await self._commitments.on_extracted(event_id, found)
        except Exception as exc:  # noqa: BLE001 - never block the chunks or retry a paid call
            # Type name only: the message may carry untrusted text (INV-7).
            log.warning("commitment record failed err=%s", type(exc).__name__)

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
        commitment_cue = self._commitments is not None and has_commitment_cue(body)
        if (len(body) < self._min_chars and not commitment_cue) or is_low_signal(body):
            log.info("memory_extract skipped: not worth a model call")
            return
        if self._mode == "cue" and not (commitment_cue or has_memory_cue(body)):
            log.info("memory_extract skipped: no memory cue")
            return

        # Nothing below may spend (the API embedder and the model both do).
        if await self._llm.mode() is not LLMMode.NORMAL:
            raise self._defer("model_off")
        if self._spend is not None and await self._spend.fraction_used() > self._share:
            raise self._defer("spend_share")

        try:
            await self._write(event_id, [ChunkIn(text=_clip(body), chunk_index=0, importance=0.5)])
            if extracted:
                return  # an earlier run finished the extraction: no second model spend
            transcript = Transcript(
                segments=[Segment(text=_clip(body))],  # bounded input: cost is capped (R5)
                source_model="user_text",
                untrusted=False,
                created_at=ev.occurred_at,
            )
            items = await CombinedExtractor(self._llm).extract(transcript, source_event_id=event_id)
            numbered: list[ChunkIn] = []
            for item in items:
                line = _line(item)
                if line and line.strip() and len(numbered) < MAX_EXTRACTED_CHUNKS:
                    conf = getattr(item, "confidence", None)
                    numbered.append(
                        ChunkIn(
                            text=line,
                            chunk_index=len(numbered) + 1,
                            importance=_importance(item),
                            confidence=conf if isinstance(conf, float) else None,
                        )
                    )
            # Before the chunk write: a crash in between replays the extraction, and the
            # ledger's dedupe key makes the second pass a no-op.
            await self._record_commitments(event_id, items)
            if numbered:
                await self._write(event_id, numbered)
        except LLMRefused as exc:
            if exc.reason == "scrub_blocked":
                raise PermanentJobError("scrub_blocked") from None
            if exc.reason in ("kill_switch", "spend_cap", "rate_limited"):
                raise self._defer(exc.reason) from None
            raise  # guard_error and the like: transient, retried with backoff
        except LLMProviderError as exc:
            if exc.code == "rate_limited":
                raise self._defer("rate_limited") from None
            raise  # transient: the runner backs off
        except LLMResponseError:
            raise PermanentJobError("bad_response") from None
        log.info("memory_extract done chunks=%d", 1 + len(numbered))


class MemoryExtractSweeper:
    """The `Scheduler` seam (`tick()`): a backstop for the post-turn enqueue. It
    enqueues `memory_extract` for every trusted user text / voice-transcript
    event of the last `lookback` that has no live job (a crash between the turn
    and the enqueue, and voice transcripts, which start no turn). Untrusted events
    are never selected. Idempotent (the job's dedupe key is the event id) and
    rate-limited to `max_per_min` enqueues a minute (token bucket on the injected
    clock), so catching up after downtime cannot be a spending spike."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: JobQueue,
        clock: Clock,
        *,
        lookback: timedelta = SWEEP_LOOKBACK,
        max_per_min: int = MAX_ENQUEUE_PER_MIN,
        retry_dead_after: timedelta = RETRY_DEAD_AFTER,
        purge_after: timedelta = PURGE_AFTER,
        purge_every: timedelta = PURGE_EVERY,
        min_chars: int = MIN_CHARS,
    ) -> None:
        self._min_chars = min_chars
        if purge_after <= lookback:
            raise ValueError("purge_after must exceed the sweep look-back")
        self._purge_after = purge_after
        self._purge_every = purge_every
        self._last_purge: datetime | None = None
        self._sf = session_factory
        self._q = queue
        self._clock = clock
        self._lookback = lookback
        self._rate = max_per_min
        self._retry_after = retry_dead_after
        self._tokens = float(max_per_min)
        self._last: datetime | None = None

    def _allowance(self) -> int:
        now = self._clock.now()
        if self._last is not None:
            elapsed = max(0.0, (now - self._last).total_seconds())
            self._tokens = min(float(self._rate), self._tokens + elapsed * self._rate / 60.0)
        self._last = now
        return int(self._tokens)

    async def _maybe_purge(self) -> None:
        now = self._clock.now()
        if self._last_purge is not None and now - self._last_purge < self._purge_every:
            return
        self._last_purge = now
        removed = await self._q.purge_finished(older_than=self._purge_after)
        if removed:
            log.info("purged %d finished job(s)", removed)

    async def tick(self) -> None:
        await self._maybe_purge()
        allowed = self._allowance()
        if allowed < 1:
            return
        now = self._clock.now()
        key = literal(f"{KIND}:") + cast(EventRow.id, String)
        # A job blocks re-enqueueing unless it is dead of a transient cause and the
        # cool-down has passed.
        blocking = or_(
            Job.status.in_(("done", "pending", "running", "cancelled", "failed")),
            and_(
                Job.status == "dead",
                or_(
                    Job.last_error.in_(PERMANENT_ERRORS),
                    cast(func.coalesce(Job.payload["revivals"].astext, "0"), Integer)
                    >= MAX_REVIVALS,
                    Job.updated_at > now - self._retry_after,
                ),
            ),
        )
        async with self._sf() as session, session.begin():
            missing = list(
                await session.scalars(
                    select(EventRow.id)
                    .where(
                        EventRow.lane == "trusted",
                        EventRow.kind.in_(sorted(EXTRACTABLE_KINDS)),
                        EventRow.created_at >= now - self._lookback,
                        func.char_length(func.coalesce(EventRow.body, "")) >= self._min_chars,
                        ~exists().where(Job.dedupe_key == key, blocking),
                    )
                    .order_by(EventRow.created_at)
                    .limit(allowed)
                )
            )
            found = await session.execute(
                select(Job.dedupe_key, Job.payload).where(
                    Job.dedupe_key.in_([dedupe_key(e) for e in missing])
                )
            )
            old: dict[str, dict[str, Any]] = {k: dict(v) for k, v in found.tuples().all()}
            for event_id in missing:
                prior = old.get(dedupe_key(event_id))
                count = int(prior.get("revivals", 0)) + 1 if prior is not None else 0
                await enqueue_memory_extract(self._q, session, event_id, rearm=True, revivals=count)
        self._tokens -= len(missing)
