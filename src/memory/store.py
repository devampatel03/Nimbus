# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Episodic `MemoryStore` (P1.3b, P2.4; ADR-0001, ADR-0017, TARGET_DESIGN
section 3): a Protocol plus the pgvector implementation over `memory_chunk`.

Provenance (R7/INV-5): the store never invents events. The caller records its
real `Event` (`record_event`, idempotent on `source_key`, e.g.
`telegram:<update_id>`) and attaches chunks to that `event_id` (`add_chunks`,
idempotent on `(event_id, chunk_index)`). A chunk inherits `lane` from its
event and cannot override it; `confidence` may only be lowered.

R8 / INV-6: chunks from `lane='untrusted'` events are inserted `promoted=false`
and `search` excludes unpromoted rows unless `MemoryQuery.include_unpromoted`
(review tooling only). Every hit carries `lane`/`untrusted`/`promoted` so
callers render untrusted hits as DATA blocks. `promote` needs the id of a
*consumed* user approval (`pending_action.status='consumed'`) and stores it in
`promoted_approval_id` (DB CHECK + set-once trigger back this up).

Bi-temporal reads (INV-3, append-only; nothing is ever updated in place):

* valid time    `MemoryQuery.as_of`:   `event.occurred_at <= as_of`.
* record time   `MemoryQuery.known_at`: `created_at <= known_at` and not yet
  invalidated at `known_at` (`invalidated_at` is NULL or `> known_at`).
  With `known_at=None` only rows that are not invalidated now are visible.
Supersession = `invalidate(old, superseded_by=new)`: sets `invalidated_at` /
`superseded_by` on the old row only (the only mutable columns, enforced by the
`nimbus_append_only` trigger); content is never touched.

Rerank: pgvector cosine candidates (3x pool) are rescored with
`retriever.score` (similarity + recency + importance [+ speaker]); recency is
measured against `as_of` when given, so a past-dated query ranks the way it
would have then.

The session is the caller's: methods flush, never commit.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.contracts.event import Event as EventContract
from src.contracts.memory_query import MemoryQuery
from src.memory.retriever import (
    DEFAULT_WEIGHTS,
    RetrievalWeights,
    ScoringTerms,
    age_days,
    score,
    set_overlap,
)
from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.memory_chunk import EMBED_DIM, MemoryChunk

CANDIDATE_POOL_FACTOR = 3


class StoreError(Exception):
    """Base class; messages are fixed codes (no content, INV-7)."""


class EventNotFoundError(StoreError):
    pass


class ChunkNotFoundError(StoreError):
    pass


class ApprovalRequiredError(StoreError):
    """Promotion without a consumed user approval (R8)."""


class ChunkIn(BaseModel):
    """One chunk to store. Lane is NOT a field: it is the event's (R8)."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    chunk_index: int | None = Field(default=None, ge=0)  # None = position in the call
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)  # None = event's; never raised


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: uuid.UUID
    content: str
    similarity: float  # cosine similarity in [-1, 1]
    score: float  # reranked (similarity + recency + importance + speaker)
    importance: float
    confidence: float
    occurred_at: datetime
    created_at: datetime
    session_id: uuid.UUID  # meta.session_id if the event has one, else the event id
    speaker_id: uuid.UUID | None  # from event meta, if the caller recorded one
    source_event_id: uuid.UUID
    lane: str  # 'trusted' | 'untrusted'
    untrusted: bool  # INV-6: render as a DATA block when True
    promoted: bool


MemoryHit = ChunkHit


class VectorEmbedder(Protocol):
    """What the store needs from an embedder (src.memory.embedder.Qwen3Embedder
    and src.llm.embeddings_api.ApiEmbedder both satisfy it)."""

    model_id: str
    dim: int

    async def aembed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...


class MemoryStore(Protocol):
    async def record_event(self, event: EventContract) -> uuid.UUID: ...

    async def add_chunks(
        self, event_id: uuid.UUID, chunks: Sequence[ChunkIn]
    ) -> list[uuid.UUID]: ...

    async def search(self, q: MemoryQuery) -> list[ChunkHit]: ...

    async def invalidate(
        self, chunk_id: uuid.UUID, *, superseded_by: uuid.UUID | None = None
    ) -> None: ...

    async def promote(self, item_id: uuid.UUID, *, approval_id: uuid.UUID) -> None: ...


# All values are bound parameters; the only text in the statements is these
# fixed fragments. NULL :asof / :ka = that axis is unconstrained.
_SEARCH_SQL = """
SELECT id, content, similarity, importance, confidence, occurred_at, created_at, meta,
       source_event_id, lane, promoted
FROM (
    SELECT c.id, c.content, 1 - (c.embedding <=> CAST(:q AS vector)) AS similarity,
           c.importance, c.confidence, e.occurred_at, c.created_at, e.meta,
           c.source_event_id, c.lane, c.promoted,
           c.embedding <=> CAST(:q AS vector) AS dist
    FROM memory_chunk c
    JOIN event e ON e.id = c.source_event_id
    WHERE (c.promoted OR :unp)
      AND (CAST(:asof AS timestamptz) IS NULL OR e.occurred_at <= CAST(:asof AS timestamptz))
      AND CASE WHEN CAST(:ka AS timestamptz) IS NULL THEN c.invalidated_at IS NULL
               ELSE c.created_at <= CAST(:ka AS timestamptz)
                    AND (c.invalidated_at IS NULL OR c.invalidated_at > CAST(:ka AS timestamptz))
          END
    ORDER BY c.embedding <=> CAST(:q AS vector)
    LIMIT :n
) t
ORDER BY dist
"""

_INVALIDATE_SQL = """
UPDATE memory_chunk SET invalidated_at = clock_timestamp(), superseded_by = :sb
WHERE id = :i AND invalidated_at IS NULL
RETURNING id
"""

_PROMOTE_SQL = """
UPDATE memory_chunk SET promoted = true, promoted_approval_id = :a
WHERE id = :i AND promoted = false
RETURNING id
"""

_APPROVAL_CONSUMED_SQL = (
    "SELECT 1 FROM pending_action WHERE id = :a AND status = 'consumed' AND consumed_at IS NOT NULL"
)

_GET_SCAN = "SELECT current_setting('hnsw.iterative_scan', true)"
_SET_SCAN = "SELECT set_config('hnsw.iterative_scan', :v, true)"


def vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorMemoryStore:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder: VectorEmbedder,
        weights: RetrievalWeights = DEFAULT_WEIGHTS,
    ) -> None:
        if embedder.dim != EMBED_DIM:
            raise ValueError(
                f"embedder dim {embedder.dim} != memory_chunk vector({EMBED_DIM}): "
                "schema migration and re-embed job required"
            )
        self._session = session
        self._embedder = embedder
        self._weights = weights

    # -- events ----------------------------------------------------------

    async def record_event(self, event: EventContract) -> uuid.UUID:
        """Idempotent on `source_key`; returns the existing id on replay."""
        stmt = (
            pg_insert(EventRow)
            .values(
                source_key=event.source_key,
                kind=event.kind.value,
                source=event.source,
                lane=event.lane,
                confidence=event.confidence,
                body=event.body,
                meta=event.meta,
                file_unique_id=event.file_unique_id,
                occurred_at=event.occurred_at,
            )
            .on_conflict_do_nothing(index_elements=[EventRow.source_key])
            .returning(EventRow.id)
        )
        new_id = (await self._session.execute(stmt)).scalar_one_or_none()
        if new_id is not None:
            return new_id
        existing = await self._session.scalar(
            select(EventRow.id).where(EventRow.source_key == event.source_key)
        )
        assert existing is not None  # conflict implies the row exists
        return existing

    # -- writes ----------------------------------------------------------

    async def add_chunks(self, event_id: uuid.UUID, chunks: Sequence[ChunkIn]) -> list[uuid.UUID]:
        """Embed and store `chunks` against the caller's event. Idempotent on
        `(event_id, chunk_index)`: chunks already stored are neither
        re-embedded nor rewritten. Returns ids in input order."""
        if not chunks:
            return []
        ev = (
            await self._session.execute(
                select(EventRow.lane, EventRow.confidence).where(EventRow.id == event_id)
            )
        ).one_or_none()
        if ev is None:
            raise EventNotFoundError("event_not_found")
        indices = [c.chunk_index if c.chunk_index is not None else i for i, c in enumerate(chunks)]
        if len(set(indices)) != len(indices):
            raise ValueError("duplicate chunk_index in one call")

        have = await self._ids_by_index(event_id, indices)
        todo = sorted(
            ((i, c) for i, c in zip(indices, chunks, strict=True) if i not in have),
            key=lambda t: t[0],  # stable order: concurrent writers cannot deadlock
        )
        if todo:
            vectors = await self._embedder.aembed_batch([c.text for _, c in todo])
            if len(vectors) != len(todo):
                raise ValueError("embedder returned the wrong number of vectors")
            rows = []
            for (idx, c), vec in zip(todo, vectors, strict=True):
                if len(vec) != self._embedder.dim:
                    raise ValueError(f"expected {self._embedder.dim} dims, got {len(vec)}")
                conf = ev.confidence if c.confidence is None else min(c.confidence, ev.confidence)
                rows.append(
                    {
                        "source_event_id": event_id,
                        "chunk_index": idx,
                        "content": c.text,
                        "embedding": vec,
                        "model_id": self._embedder.model_id,
                        "dim": self._embedder.dim,
                        "lane": ev.lane,
                        "confidence": conf,
                        "importance": c.importance,
                        "promoted": ev.lane == "trusted",
                    }
                )
            await self._session.execute(
                pg_insert(MemoryChunk)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["source_event_id", "chunk_index"])
            )
            await self._session.flush()
        have = await self._ids_by_index(event_id, indices)
        return [have[i] for i in indices]

    async def _ids_by_index(self, event_id: uuid.UUID, indices: list[int]) -> dict[int, uuid.UUID]:
        rows = (
            await self._session.execute(
                select(MemoryChunk.chunk_index, MemoryChunk.id).where(
                    MemoryChunk.source_event_id == event_id, MemoryChunk.chunk_index.in_(indices)
                )
            )
        ).all()
        return {r.chunk_index: r.id for r in rows}

    # -- reads -----------------------------------------------------------

    async def search(self, q: MemoryQuery) -> list[ChunkHit]:
        [vec] = await self._embedder.aembed_batch([q.query_text])
        if len(vec) != self._embedder.dim:
            raise ValueError(f"expected {self._embedder.dim} dims, got {len(vec)}")
        params = {
            "q": vector_literal(vec),
            "n": max(q.top_k * CANDIDATE_POOL_FACTOR, q.top_k),
            "unp": q.include_unpromoted,
            "asof": q.as_of,
            "ka": q.known_at,
        }
        # Filters (promoted / as_of / known_at) run after the HNSW scan, which
        # would under-return; iterative scan keeps going until `n` rows survive.
        # The setting is transaction-scoped, so restore the prior value.
        prior = (await self._session.execute(text(_GET_SCAN))).scalar_one_or_none() or "off"
        await self._session.execute(text(_SET_SCAN), {"v": "relaxed_order"})
        try:
            rows = (await self._session.execute(text(_SEARCH_SQL), params)).all()
        finally:
            await self._session.execute(text(_SET_SCAN), {"v": prior})

        reference = q.as_of or datetime.now(UTC)
        present = frozenset(str(s) for s in q.present_speakers)
        hits: list[ChunkHit] = []
        for r in rows:
            speaker = r.meta.get("speaker_id")
            occurred = r.occurred_at.astimezone(UTC)
            terms = ScoringTerms(
                cosine_sim=float(r.similarity),
                graph_proximity=0.0,
                age_days=age_days(occurred, reference),
                importance=r.importance,
                speaker_relevance=set_overlap(
                    frozenset({str(speaker)}) if speaker else frozenset(), present
                ),
                verified_bonus=0.0,
            )
            sess = r.meta.get("session_id")
            hits.append(
                ChunkHit(
                    chunk_id=r.id,
                    content=r.content,
                    similarity=float(r.similarity),
                    score=score(terms, self._weights),
                    importance=r.importance,
                    confidence=r.confidence,
                    occurred_at=occurred,
                    created_at=r.created_at.astimezone(UTC),
                    session_id=uuid.UUID(sess) if sess else r.source_event_id,
                    speaker_id=uuid.UUID(speaker) if speaker else None,
                    source_event_id=r.source_event_id,
                    lane=r.lane,
                    untrusted=r.lane == "untrusted",
                    promoted=r.promoted,
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[: q.top_k]

    async def stored_models(self) -> set[tuple[str, int]]:
        """Distinct (model_id, dim) among live rows, for `check_index_compat`."""
        rows = (
            await self._session.execute(
                text("SELECT DISTINCT model_id, dim FROM memory_chunk WHERE invalidated_at IS NULL")
            )
        ).all()
        return {(r.model_id, r.dim) for r in rows}

    # -- supersession / promotion ------------------------------------------

    async def invalidate(
        self, chunk_id: uuid.UUID, *, superseded_by: uuid.UUID | None = None
    ) -> None:
        """Record-time retraction: stamps `invalidated_at` (and `superseded_by`)
        on the old row. Unknown id raises; an already-invalidated row is a no-op
        (the column is set-once)."""
        done = (
            await self._session.execute(text(_INVALIDATE_SQL), {"i": chunk_id, "sb": superseded_by})
        ).first()
        if done is None and not await self._exists(chunk_id):
            raise ChunkNotFoundError("chunk_not_found")

    async def promote(self, item_id: uuid.UUID, *, approval_id: uuid.UUID) -> None:
        """R8: only with a consumed user approval; never from model output."""
        ok = (await self._session.execute(text(_APPROVAL_CONSUMED_SQL), {"a": approval_id})).first()
        if ok is None:
            raise ApprovalRequiredError("consumed_approval_required")
        done = (
            await self._session.execute(text(_PROMOTE_SQL), {"i": item_id, "a": approval_id})
        ).first()
        if done is None and not await self._exists(item_id):
            raise ChunkNotFoundError("chunk_not_found")

    async def _exists(self, chunk_id: uuid.UUID) -> bool:
        found = await self._session.scalar(select(MemoryChunk.id).where(MemoryChunk.id == chunk_id))
        return found is not None
