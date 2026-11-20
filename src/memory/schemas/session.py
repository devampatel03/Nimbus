# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Session model — persisted counterpart of the `SessionBundle` contract
(src/contracts/session_bundle.py). `graph_edge.source_session` and the
commitment/decision ledgers reference `session.id` for provenance (INV-5).

Not spelled out in docs/TECHNICAL_ARCHITECTURE.md's SQL section; fields are
the subset of `SessionBundle` relevant to long-term provenance, not the
full capture-time payload (audio_ref/codec/sample_rate live in the
perception pipeline, not the memory store).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)


class Session(Base):
    __tablename__ = "session"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    device_id: Mapped[str] = mapped_column(nullable=False)
