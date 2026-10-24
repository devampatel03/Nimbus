# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Decision journal (docs/EXECUTION_PLAN.md T5.2/T5.3): captures decisions
with reasoning and a review horizon, plus the later retrospective outcome.

Not spelled out in docs/TECHNICAL_ARCHITECTURE.md's SQL section — T5.2/T5.3
are P5-phase tasks; this is a minimal, defensible design built now per this
session's explicit instruction. `source_session` is non-null per INV-5.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base
from src.memory.schemas.provenance import CONFIDENCE_CHECK_SQL, lane_check, promotion_check

_TZ = DateTime(timezone=True)


class Decision(Base):
    __tablename__ = "decision"
    __table_args__ = (
        lane_check("decision"),
        promotion_check("decision"),
        CheckConstraint(CONFIDENCE_CHECK_SQL, name="ck_decision_confidence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_session: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("session.id"), nullable=False
    )
    decision_text: Mapped[str] = mapped_column(nullable=False)
    reasoning: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    review_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    outcome: Mapped[str | None] = mapped_column(nullable=True)
    outcome_captured_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)

    # P1.2c provenance (R7/R8). Legacy writers omit these; server defaults keep them valid.
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    lane: Mapped[str] = mapped_column(nullable=False, server_default=text("'trusted'"))
    promoted: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    promoted_approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
