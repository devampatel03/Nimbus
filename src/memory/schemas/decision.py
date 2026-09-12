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

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)


class Decision(Base):
    __tablename__ = "decision"

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
