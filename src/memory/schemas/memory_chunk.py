# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`memory_chunk` (P1.2c, TARGET_DESIGN §6, ADR-0001/0013): the one episodic
table. `vector(1024)` embedding (Qwen3-Embedding-0.6B native dim); `model_id`
and `dim` are stored per row so an embedding change is a detectable re-embed
migration, never a silent mismatch. Changing the dimension requires a new
migration (column type + HNSW index rebuild) and a re-embed job.

Index choice: HNSW with `vector_cosine_ops` (m=16, ef_construction=64).
HNSW needs no training pass (IVFFlat needs representative data to build its
lists, which a new single-user DB does not have), has better recall/latency
at this scale (<~1M rows), and pgvector HNSW supports <=2000 dims. Cost:
slower build and more RAM than IVFFlat -- acceptable at personal scale.

R8: untrusted chunks are inserted with `promoted=false` and can only become
`promoted=true` with a `promoted_approval_id` (CHECK). Append-only (trigger):
only invalidated_at/superseded_by/promoted* may change.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base
from src.memory.schemas.provenance import confidence_check, lane_check, promotion_check
from src.memory.schemas.vector import Vector

_TZ = DateTime(timezone=True)
EMBED_DIM = 1024


class MemoryChunk(Base):
    __tablename__ = "memory_chunk"
    __table_args__ = (
        lane_check("memory_chunk"),
        confidence_check("memory_chunk"),
        promotion_check("memory_chunk"),
        CheckConstraint("importance BETWEEN 0 AND 1", name="ck_memory_chunk_importance"),
        CheckConstraint(f"dim = {EMBED_DIM}", name="ck_memory_chunk_dim"),
        UniqueConstraint("source_event_id", "chunk_index", name="uq_memory_chunk_event_idx"),
        Index(
            "idx_memory_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("idx_memory_chunk_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    source_event_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    content: Mapped[str] = mapped_column(nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=False)
    model_id: Mapped[str] = mapped_column(nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text(str(EMBED_DIM)))
    lane: Mapped[str] = mapped_column(nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    importance: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.5"))
    # Default true so trusted rows are searchable; untrusted inserts must set
    # false explicitly (CHECK rejects untrusted+promoted without approval).
    promoted: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    promoted_approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    invalidated_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory_chunk.id"), nullable=True
    )
