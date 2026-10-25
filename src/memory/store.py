# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Episodic `MemoryStore` (P1.3b, ADR-0001, TARGET_DESIGN §6): a Protocol plus
the pgvector implementation over `memory_chunk` (P1.2c). Replaces Qdrant.

Provenance (R7/INV-5): each chunk is written with its own `event` row (the
provenance root; carries lane, confidence, occurred_at, and the originating
session/speaker in `meta`), and the chunk row carries lane, confidence,
created_at, source_event_id, model_id and dim.

R8: chunks with `lane='untrusted'` are inserted `promoted=false` and are never
returned by `search`; promotion needs `promoted_approval_id` (DB CHECK) and is
outside this module. Append-only: this module never UPDATEs content; the only
mutation is `invalidate` (sets `invalidated_at`, allowed by the trigger).

The session is the caller's: methods flush, never commit.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.schemas.event import Event
from src.memory.schemas.memory_chunk import EMBED_DIM, MemoryChunk


class EpisodicChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    speaker_id: uuid.UUID | None = None
    occurred_at: datetime
    importance: float = 0.5
    confidence: float = 1.0
    lane: str = "trusted"  # 'trusted' | 'untrusted' (R8)


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: uuid.UUID
    content: str
    similarity: float  # cosine similarity in [-1, 1]
    importance: float
    confidence: float
    occurred_at: datetime
    session_id: uuid.UUID
    speaker_id: uuid.UUID | None
    source_event_id: uuid.UUID


class MemoryStore(Protocol):
    async def write_chunks(
        self, session_id: uuid.UUID, items: Sequence[tuple[EpisodicChunk, list[float]]]
    ) -> list[uuid.UUID]: ...

    async def search(
        self, query_vec: list[float], *, limit: int, session_id: uuid.UUID | None = None
    ) -> list[ChunkHit]: ...

    async def invalidate(self, chunk_id: uuid.UUID) -> None: ...


_SEARCH_SQL = """
SELECT id, content, similarity, importance, confidence, occurred_at, meta, source_event_id
FROM (
    SELECT c.id, c.content, 1 - (c.embedding <=> CAST(:q AS vector)) AS similarity,
           c.importance, c.confidence, e.occurred_at, e.meta, c.source_event_id,
           c.embedding <=> CAST(:q AS vector) AS dist
    FROM memory_chunk c
    JOIN event e ON e.id = c.source_event_id
    WHERE c.promoted AND c.invalidated_at IS NULL
      AND (CAST(:sid AS text) IS NULL OR e.meta->>'session_id' = CAST(:sid AS text))
    ORDER BY c.embedding <=> CAST(:q AS vector)
    LIMIT :n
) t
ORDER BY dist
"""


class PgVectorMemoryStore:
    def __init__(self, session: AsyncSession, *, model_id: str, dim: int = EMBED_DIM) -> None:
        self._session = session
        self._model_id = model_id
        self._dim = dim

    async def write_chunks(
        self, session_id: uuid.UUID, items: Sequence[tuple[EpisodicChunk, list[float]]]
    ) -> list[uuid.UUID]:
        ids: list[uuid.UUID] = []
        for chunk, vector in items:
            if len(vector) != self._dim:
                raise ValueError(f"expected {self._dim} dims, got {len(vector)}")
            event = Event(
                source_key=f"episodic:{session_id}:{uuid.uuid4()}",
                kind="episodic_chunk",
                source="memory_store",
                lane=chunk.lane,
                confidence=chunk.confidence,
                occurred_at=chunk.occurred_at,
                meta={
                    "session_id": str(session_id),
                    "speaker_id": str(chunk.speaker_id) if chunk.speaker_id else None,
                },
            )
            self._session.add(event)
            await self._session.flush()
            row = MemoryChunk(
                source_event_id=event.id,
                chunk_index=0,
                content=chunk.text,
                embedding=vector,
                model_id=self._model_id,
                dim=self._dim,
                lane=chunk.lane,
                confidence=chunk.confidence,
                importance=chunk.importance,
                promoted=chunk.lane == "trusted",
            )
            self._session.add(row)
            await self._session.flush()
            ids.append(row.id)
        return ids

    async def search(
        self, query_vec: list[float], *, limit: int, session_id: uuid.UUID | None = None
    ) -> list[ChunkHit]:
        if len(query_vec) != self._dim:
            raise ValueError(f"expected {self._dim} dims, got {len(query_vec)}")
        if session_id is not None:
            # A filtered HNSW scan would otherwise under-return (post-filter).
            await self._session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        vec = "[" + ",".join(repr(float(x)) for x in query_vec) + "]"
        rows = (
            await self._session.execute(
                text(_SEARCH_SQL),
                {"q": vec, "n": limit, "sid": str(session_id) if session_id else None},
            )
        ).all()
        hits = []
        for r in rows:
            speaker = r.meta.get("speaker_id")
            hits.append(
                ChunkHit(
                    chunk_id=r.id,
                    content=r.content,
                    similarity=float(r.similarity),
                    importance=r.importance,
                    confidence=r.confidence,
                    occurred_at=r.occurred_at.astimezone(UTC),
                    session_id=uuid.UUID(r.meta["session_id"]),
                    speaker_id=uuid.UUID(speaker) if speaker else None,
                    source_event_id=r.source_event_id,
                )
            )
        return hits

    async def invalidate(self, chunk_id: uuid.UUID) -> None:
        await self._session.execute(
            text("UPDATE memory_chunk SET invalidated_at = now() WHERE id = :i"),
            {"i": chunk_id},
        )
