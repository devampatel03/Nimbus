# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Unified memory manager (docs/TECHNICAL_ARCHITECTURE.md section 6, MEM-1):
episodic (pgvector `memory_chunk`) and semantic (Postgres graph) tiers
behind one interface.

P2.4 notes:

- Episodic writes take the caller's real `event_id` (`record_event` first;
  idempotent on `source_key`); the manager never mints events (P1.3b MED-1).
  Embeddings are batched (`aembed_batch`, one call per write) and the model id
  and dim come from the embedder, not a guess.
- `verify_index()` runs `check_index_compat` against the model/dim stored in
  `memory_chunk`; it is called by app startup and, as a backstop, lazily once
  before the first query/write, so mixing embedders cannot happen silently.
- Retrieval is bi-temporal (`MemoryQuery.as_of` valid time, `.known_at`
  record time) and R8-filtered (unpromoted rows excluded unless
  `include_unpromoted`). `MemoryResult.lane/untrusted` are populated so
  callers can render untrusted results as DATA blocks (INV-6).
- Semantic (graph) results are scored with `retriever.score` using entity
  proximity; episodic results are scored in the store. Episodic candidates
  carry no entity links yet, so their `graph_proximity` is 0.0.
- `DeterministicStubEmbedder` is for tests (hash vectors, NOT semantic). The
  default embedder is `Qwen3Embedder` (lazy; never loads at construction).
- Single-user deployment (PRD section 1): no per-user filter at this layer.
"""

import hashlib
import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.contracts.event import Event
from src.contracts.memory_query import MemoryQuery, MemoryResult
from src.memory.embedder import DEFAULT_EMBED_DIM, BatchEmbedder, Qwen3Embedder, check_index_compat
from src.memory.graph import GraphRepository
from src.memory.retriever import (
    DEFAULT_WEIGHTS,
    RetrievalWeights,
    ScoringTerms,
    age_days,
    score,
    set_overlap,
)
from src.memory.schemas.graph import GraphEdge, GraphNode
from src.memory.store import (
    ChunkHit,
    ChunkIn,
    MemoryStore,
    PgVectorMemoryStore,
)

EMBED_DIM = DEFAULT_EMBED_DIM

__all__ = [
    "ChunkIn",
    "DeterministicStubEmbedder",
    "Embedder",
    "MemoryManager",
    "SemanticFact",
]


class Embedder(Protocol):
    """Sync single-text embedder (legacy shape, kept for callers)."""

    def embed(self, text: str) -> list[float]: ...


class DeterministicStubEmbedder:
    """Text-derived unit vector; deterministic, NOT semantically meaningful.
    For tests/plumbing only -- production uses `Qwen3Embedder` by default.
    """

    model_id = "DeterministicStubEmbedder"
    dim = EMBED_DIM

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [float(digest[i % len(digest)]) for i in range(EMBED_DIM)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)

    async def aembed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_batch(texts)


class SemanticFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject_id: uuid.UUID
    predicate: str
    object_id: uuid.UUID | None = None
    object_literal: str | None = None
    valid_from: datetime
    source_session: uuid.UUID
    confidence: float
    source_speaker: uuid.UUID | None = None


class MemoryManager:
    # (model_id, dim) pairs already verified against the stored index in this
    # process, so the lazy per-request guard does not re-run SELECT DISTINCT.
    _verified: ClassVar[set[tuple[str, int]]] = set()

    def __init__(
        self,
        *,
        pg_session: AsyncSession,
        store: MemoryStore | None = None,
        embedder: BatchEmbedder | None = None,
        weights: RetrievalWeights = DEFAULT_WEIGHTS,
    ) -> None:
        self._session = pg_session
        self._embedder: BatchEmbedder = embedder or Qwen3Embedder()
        self._store: MemoryStore = store or PgVectorMemoryStore(
            pg_session, embedder=self._embedder, weights=weights
        )
        self._graph_repo = GraphRepository(pg_session)
        self._weights = weights
        self._index_checked = False

    # -- startup guard ----------------------------------------------------

    async def verify_index(self) -> None:
        """Refuse to run against an index built by another model/dim
        (`EmbeddingMigrationRequired`): a model change needs a migration and a
        re-embed job. Call at app startup; also enforced lazily once."""
        stored = getattr(self._store, "stored_models", None)
        if stored is not None:
            for model_id, dim in sorted(await stored()):
                check_index_compat(self._embedder, stored_model_id=model_id, stored_dim=dim)
        self._index_checked = True
        self._verified.add((self._embedder.model_id, self._embedder.dim))

    async def _ensure_index_checked(self) -> None:
        key = (self._embedder.model_id, self._embedder.dim)
        if not self._index_checked and key not in self._verified:
            await self.verify_index()
        self._index_checked = True

    # -- episodic (pgvector) ------------------------------------------

    async def record_event(self, event: Event) -> uuid.UUID:
        """Persist the provenance root; idempotent on `event.source_key`."""
        return await self._store.record_event(event)

    async def write_episodic(
        self, event_id: uuid.UUID, chunks: Sequence[ChunkIn]
    ) -> list[uuid.UUID]:
        """Embed (one batch) and store chunks against the caller's real event.
        Idempotent on `(event_id, chunk_index)`."""
        await self._ensure_index_checked()
        return await self._store.add_chunks(event_id, chunks)

    async def promote(self, item_id: uuid.UUID, *, approval_id: uuid.UUID) -> None:
        """R8: needs a consumed user approval id (never model output)."""
        await self._store.promote(item_id, approval_id=approval_id)

    async def invalidate(
        self, chunk_id: uuid.UUID, *, superseded_by: uuid.UUID | None = None
    ) -> None:
        await self._store.invalidate(chunk_id, superseded_by=superseded_by)

    # -- semantic (Postgres graph) --------------------------------------

    async def write_semantic(self, facts: Sequence[SemanticFact]) -> list[GraphEdge]:
        written: list[GraphEdge] = []
        for fact in facts:
            existing = await self._current_edge(fact.subject_id, fact.predicate)
            if existing is not None:
                if (
                    existing.object_id == fact.object_id
                    and existing.object_literal == fact.object_literal
                ):
                    written.append(existing)  # already current, idempotent no-op
                    continue
                edge = await self._graph_repo.supersede(
                    existing,
                    object_id=fact.object_id,
                    object_literal=fact.object_literal,
                    valid_from=fact.valid_from,
                    confidence=fact.confidence,
                )
            else:
                edge = await self._graph_repo.insert_fact(
                    subject_id=fact.subject_id,
                    predicate=fact.predicate,
                    object_id=fact.object_id,
                    object_literal=fact.object_literal,
                    valid_from=fact.valid_from,
                    source_session=fact.source_session,
                    confidence=fact.confidence,
                    source_speaker=fact.source_speaker,
                )
            written.append(edge)
        return written

    async def _current_edge(self, subject_id: uuid.UUID, predicate: str) -> GraphEdge | None:
        stmt = select(GraphEdge).where(
            GraphEdge.subject_id == subject_id,
            GraphEdge.predicate == predicate,
            GraphEdge.valid_to.is_(None),
            GraphEdge.invalidated_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # -- hybrid retrieval -------------------------------------------------

    async def query(self, text: str, context: MemoryQuery) -> list[MemoryResult]:
        await self._ensure_index_checked()
        now = datetime.now(UTC)
        request = context.model_copy(update={"query_text": text})
        episodic = [self._episodic_result(h) for h in await self._store.search(request)]
        semantic = await self._query_semantic(request, now)
        combined = episodic + semantic
        combined.sort(key=lambda r: r.score, reverse=True)
        return combined[: context.top_k]

    @staticmethod
    def _episodic_result(hit: ChunkHit) -> MemoryResult:
        return MemoryResult(
            memory_id=hit.chunk_id,
            content=hit.content,
            score=hit.score,
            source_session=hit.session_id,
            provenance=f"memory_chunk:{hit.chunk_id}:event:{hit.source_event_id}",
            valid_from=hit.occurred_at,
            valid_to=None,
            confidence=hit.confidence,
            lane="untrusted" if hit.untrusted else "trusted",
            untrusted=hit.untrusted,
        )

    async def _query_semantic(self, context: MemoryQuery, now: datetime) -> list[MemoryResult]:
        if not context.active_entities:
            return []
        node_ids_stmt = select(GraphNode.id).where(
            GraphNode.canonical_name.in_(context.active_entities)
        )
        node_ids = [row[0] for row in (await self._session.execute(node_ids_stmt)).all()]
        if not node_ids:
            return []

        subject_node = aliased(GraphNode)
        object_node = aliased(GraphNode)
        succ = aliased(GraphEdge)
        ka = context.known_at
        # Record time: what was known at `known_at`. An edge's valid_to only counts
        # if its successor had been recorded by then (back-dated corrections must
        # not retroactively close the old fact). No known_at = current knowledge.
        valid_to: ColumnElement[datetime | None]
        if ka is None:
            valid_to = GraphEdge.valid_to.expression
        else:
            valid_to = case(
                (succ.id.is_(None), GraphEdge.valid_to),
                (succ.ingested_at <= ka, GraphEdge.valid_to),
                else_=None,
            )
        stmt = (
            select(
                GraphEdge,
                subject_node.canonical_name,
                object_node.canonical_name,
                valid_to.label("known_valid_to"),
            )
            .join(subject_node, GraphEdge.subject_id == subject_node.id)
            .outerjoin(object_node, GraphEdge.object_id == object_node.id)
            .outerjoin(succ, succ.id == GraphEdge.superseded_by)
            .where(GraphEdge.subject_id.in_(node_ids))
        )
        if not context.include_unpromoted:
            stmt = stmt.where(GraphEdge.promoted.is_(True))  # R8
        if ka is None:
            stmt = stmt.where(GraphEdge.invalidated_at.is_(None))
        else:
            stmt = stmt.where(
                GraphEdge.ingested_at <= ka,
                (GraphEdge.invalidated_at.is_(None)) | (GraphEdge.invalidated_at > ka),
            )
        # Valid-time instant: as_of, else known_at (what was true when we asked),
        # else now. valid_from inclusive, valid_to exclusive.
        instant = context.as_of or ka or now
        stmt = stmt.where(
            GraphEdge.valid_from <= instant,
            valid_to.is_(None) | (valid_to > instant),
        )
        rows = (await self._session.execute(stmt)).all()
        reference = context.as_of or now
        present_speakers = frozenset(str(s) for s in context.present_speakers)
        results = []
        for edge, subject_name, object_name, known_valid_to in rows:
            object_repr = edge.object_literal or object_name or ""
            content = f"{subject_name} {edge.predicate} {object_repr}".strip()
            speaker_ids = (
                frozenset({str(edge.source_speaker)}) if edge.source_speaker else frozenset()
            )
            terms = ScoringTerms(
                cosine_sim=0.0,
                graph_proximity=1.0,
                age_days=age_days(edge.valid_from, reference),
                importance=edge.confidence,
                speaker_relevance=set_overlap(speaker_ids, present_speakers),
                verified_bonus=1.0 if edge.user_verified else 0.0,
            )
            untrusted = edge.lane == "untrusted"
            results.append(
                MemoryResult(
                    memory_id=edge.id,
                    content=content,
                    score=score(terms, self._weights),
                    source_session=edge.source_session,
                    provenance=f"graph_edge:{edge.id}",
                    valid_from=edge.valid_from,
                    valid_to=known_valid_to,
                    confidence=edge.confidence,
                    lane="untrusted" if untrusted else "trusted",
                    untrusted=untrusted,
                )
            )
        return results
