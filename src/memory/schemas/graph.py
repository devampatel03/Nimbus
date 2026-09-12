# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Bi-temporal graph schema (docs/TECHNICAL_ARCHITECTURE.md section 6.1).

INV-3 is enforced here: no code path may issue an UPDATE against
`GraphEdge.object_id`, `GraphEdge.object_literal` or `GraphEdge.valid_from`.
Supersession sets `valid_to`/`superseded_by` on the old row and inserts a
new one (see `src/memory/graph.py::GraphRepository`, INV-3 test).

`graph_node` is not spelled out in the architecture doc's SQL (only
`graph_edge` is) — its columns are a minimal, defensible design to give
`subject_id`/`object_id` something to reference. Flag for review if a pod
needs richer node attributes.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)


class GraphNode(Base):
    __tablename__ = "graph_node"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(nullable=False)
    canonical_name: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)


class GraphEdge(Base):
    __tablename__ = "graph_edge"
    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_graph_edge_confidence"),
        CheckConstraint(
            "object_id IS NOT NULL OR object_literal IS NOT NULL",
            name="ck_graph_edge_object_present",
        ),
        Index(
            "idx_edge_current",
            "subject_id",
            "predicate",
            postgresql_where="valid_to IS NULL AND invalidated_at IS NULL",
        ),
        Index("idx_edge_temporal", "subject_id", "valid_from", "valid_to"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph_node.id"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph_node.id"), nullable=True
    )
    object_literal: Mapped[str | None] = mapped_column(nullable=True)

    valid_from: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)

    source_session: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session.id"), nullable=False
    )
    source_speaker: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("person.id"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(nullable=False)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph_edge.id"), nullable=True
    )
    user_verified: Mapped[bool | None] = mapped_column(nullable=True, default=None)
