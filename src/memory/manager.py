# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Unified memory manager (docs/TECHNICAL_ARCHITECTURE.md section 6, MEM-1):
episodic (Qdrant) and semantic (Postgres graph) tiers
behind one interface.

Scope notes (T1.6/T1.7):

- Step 11 (resolves AD-7): the default embedder is now `Qwen3Embedder`
  (`src/memory/embedder.py`), a real CPU sentence-embedding model
  (Qwen/Qwen3-Embedding-0.6B). `DeterministicStubEmbedder` below remains
  for tests to inject explicitly via the `Embedder` protocol, so unit/
  integration tests stay fast and hermetic (no model download/load) --
  see `tests/integration/test_manager.py`, which
  now pass `embedder=DeterministicStubEmbedder()` explicitly. `write_episodic`/
  `query` run `Embedder.embed()` via `asyncio.to_thread` -- a real CPU model's
  forward pass is slow enough (seconds per call under load) that calling it
  synchronously inside these async methods blocks the event loop for the
  entire batch; this was diagnosed against a reproducible Qdrant 404 during
  the LoCoMo harness run (a collection created minutes earlier was gone by
  upsert time -- the blocked loop starved the underlying HTTP connection's
  keepalive), see MEMORY.md AD-21.
- Episodic candidates carry no entity links yet (that requires
  `src/memory/extractor.py`'s `EntityExtractor` output, not built out),
  so `graph_proximity` is always 0.0 for episodic results; only semantic
  (graph) results score on entity proximity. Flag for review once entity
  linking on episodic chunks exists.
- No per-user filter at the Qdrant query layer: Nimbus is a single-user,
  self-hosted deployment (PRD section 1), so one instance holds one user's
  data and collection-wide search is correct, not an isolation gap.
"""

import asyncio
import hashlib
import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.contracts.memory_query import MemoryQuery, MemoryResult
from src.memory.embedder import DEFAULT_EMBED_DIM, Qwen3Embedder
from src.memory.graph import GraphRepository
from src.memory.retriever import DEFAULT_WEIGHTS, RetrievalWeights, ScoringTerms, score, set_overlap
from src.memory.schemas.graph import GraphEdge, GraphNode

EPISODIC_COLLECTION = "episodic_memory"
EMBED_DIM = DEFAULT_EMBED_DIM


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class DeterministicStubEmbedder:
    """Text-derived unit vector; deterministic, NOT semantically meaningful.
    Kept for tests to inject explicitly (see module docstring) — production
    code paths use `Qwen3Embedder` by default.
    """

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [float(digest[i % len(digest)]) for i in range(EMBED_DIM)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]


class EpisodicChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    speaker_id: uuid.UUID | None = None
    occurred_at: datetime
    importance: float = 0.5
    confidence: float = 1.0


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
    def __init__(
        self,
        *,
        pg_session: AsyncSession,
        qdrant: AsyncQdrantClient,
        embedder: Embedder | None = None,
        weights: RetrievalWeights = DEFAULT_WEIGHTS,
    ) -> None:
        self._session = pg_session
        self._qdrant = qdrant
        self._embedder = embedder or Qwen3Embedder()
        self._graph_repo = GraphRepository(pg_session)
        self._weights = weights

    # -- episodic (Qdrant) --------------------------------------------

    async def write_episodic(
        self, session_id: uuid.UUID, chunks: Sequence[EpisodicChunk]
    ) -> list[uuid.UUID]:
        await self._ensure_episodic_collection()
        points: list[models.PointStruct] = []
        ids: list[uuid.UUID] = []
        for chunk in chunks:
            point_id = uuid.uuid4()
            vector = await asyncio.to_thread(self._embedder.embed, chunk.text)
            payload = {
                "text": chunk.text,
                "session_id": str(session_id),  # INV-5: provenance
                "speaker_id": str(chunk.speaker_id) if chunk.speaker_id else None,
                "occurred_at": chunk.occurred_at.isoformat(),
                "importance": chunk.importance,
                "confidence": chunk.confidence,
            }
            points.append(models.PointStruct(id=str(point_id), vector=vector, payload=payload))
            ids.append(point_id)
        # Re-check/recreate right before the upsert, not just at the top of this
        # method: with a real (slow, CPU-bound) embedder, the per-chunk loop above
        # can run for minutes. Empirically reproduced on this dev machine (see
        # MEMORY.md AD-21) that the collection can report as gone by the time the
        # loop finishes, causing upsert to 404 even though it was just created.
        await self._ensure_episodic_collection()
        await self._qdrant.upsert(collection_name=EPISODIC_COLLECTION, points=points, wait=True)
        return ids

    async def _ensure_episodic_collection(self) -> None:
        if not await self._qdrant.collection_exists(EPISODIC_COLLECTION):
            await self._qdrant.create_collection(
                collection_name=EPISODIC_COLLECTION,
                vectors_config=models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
            )

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
        now = datetime.now(UTC)
        query_vec = await asyncio.to_thread(self._embedder.embed, text)
        episodic = await self._query_episodic(query_vec, context, now)
        semantic = await self._query_semantic(context, now)
        combined = episodic + semantic
        combined.sort(key=lambda r: r.score, reverse=True)
        return combined[: context.top_k]

    async def _query_episodic(
        self, query_vec: list[float], context: MemoryQuery, now: datetime
    ) -> list[MemoryResult]:
        if not await self._qdrant.collection_exists(EPISODIC_COLLECTION):
            return []
        response = await self._qdrant.query_points(
            collection_name=EPISODIC_COLLECTION,
            query=query_vec,
            limit=max(context.top_k * 3, context.top_k),
        )
        present_speakers = frozenset(str(s) for s in context.present_speakers)
        results = []
        for point in response.points:
            payload = point.payload or {}
            occurred_at = datetime.fromisoformat(payload["occurred_at"])
            age_days = max((now - occurred_at).total_seconds() / 86400, 0.0)
            speaker_id = payload.get("speaker_id")
            terms = ScoringTerms(
                cosine_sim=point.score,
                graph_proximity=0.0,
                age_days=age_days,
                importance=payload.get("importance", 0.5),
                speaker_relevance=set_overlap(
                    frozenset({speaker_id}) if speaker_id else frozenset(), present_speakers
                ),
                verified_bonus=0.0,
            )
            results.append(
                MemoryResult(
                    memory_id=uuid.UUID(str(point.id)),
                    content=payload["text"],
                    score=score(terms, self._weights),
                    source_session=uuid.UUID(payload["session_id"]),
                    provenance=f"episodic_qdrant_point:{point.id}",
                    valid_from=occurred_at,
                    valid_to=None,
                    confidence=payload.get("confidence", 1.0),
                )
            )
        return results

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
        stmt = (
            select(GraphEdge, subject_node.canonical_name, object_node.canonical_name)
            .join(subject_node, GraphEdge.subject_id == subject_node.id)
            .outerjoin(object_node, GraphEdge.object_id == object_node.id)
            .where(
                GraphEdge.subject_id.in_(node_ids),
                GraphEdge.valid_to.is_(None),
                GraphEdge.invalidated_at.is_(None),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        present_speakers = frozenset(str(s) for s in context.present_speakers)
        results = []
        for edge, subject_name, object_name in rows:
            object_repr = edge.object_literal or object_name or ""
            content = f"{subject_name} {edge.predicate} {object_repr}".strip()
            age_days = max((now - edge.valid_from).total_seconds() / 86400, 0.0)
            speaker_ids = (
                frozenset({str(edge.source_speaker)}) if edge.source_speaker else frozenset()
            )
            terms = ScoringTerms(
                cosine_sim=0.0,
                graph_proximity=1.0,
                age_days=age_days,
                importance=edge.confidence,
                speaker_relevance=set_overlap(speaker_ids, present_speakers),
                verified_bonus=1.0 if edge.user_verified else 0.0,
            )
            results.append(
                MemoryResult(
                    memory_id=edge.id,
                    content=content,
                    score=score(terms, self._weights),
                    source_session=edge.source_session,
                    provenance=f"graph_edge:{edge.id}",
                    valid_from=edge.valid_from,
                    valid_to=edge.valid_to,
                    confidence=edge.confidence,
                )
            )
        return results
