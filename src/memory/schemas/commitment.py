# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Commitment ledger (docs/EXECUTION_PLAN.md T4.8/T4.9): tracks commitments
extracted from sessions, their status and closure.

Not spelled out in docs/TECHNICAL_ARCHITECTURE.md's SQL section — T4.8/T4.9
are P4-phase tasks; this is a minimal, defensible design built now per this
session's explicit instruction. `source_session` is non-null per INV-5.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)


class Commitment(Base):
    __tablename__ = "commitment"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed', 'broken')", name="ck_commitment_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_session: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session.id"), nullable=False
    )
    owner_person_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("person.id"), nullable=True
    )
    description: Mapped[str] = mapped_column(nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, default="open")
    confidence: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    closure_source_session: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session.id"), nullable=True
    )
