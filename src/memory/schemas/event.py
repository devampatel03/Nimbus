# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""`event` (P1.2c, TARGET_DESIGN §2/§6): the provenance root. Every derived
row (memory_chunk, graph_edge, commitment, decision) points at the event it
came from. Append-only (trigger). `source_key` makes `record_event`
idempotent on replay (e.g. 'tg:update:123').

Deviation (see report): the legacy `session` table and `source_session` FKs
stay for now -- ADR-0014's session->event rename lands with the manager/graph
rewrite, because graph.py/manager.py (not owned by P1.2c) still write
`source_session`. New derived rows carry `source_event_id` in addition.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base
from src.memory.schemas.provenance import confidence_check, lane_check

_TZ = DateTime(timezone=True)


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (lane_check("event"), confidence_check("event"))

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    source_key: Mapped[str] = mapped_column(nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(nullable=False)
    lane: Mapped[str] = mapped_column(nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    body: Mapped[str | None] = mapped_column(nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    file_unique_id: Mapped[str | None] = mapped_column(nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
