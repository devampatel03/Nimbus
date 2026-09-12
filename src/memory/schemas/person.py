# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Person model with voice-print store (docs/TECHNICAL_ARCHITECTURE.md
section 5.3, speaker enrollment). `graph_edge.source_speaker` and
`commitment.owner_person_id` reference `person.id`.

Not spelled out in the architecture doc's SQL section (only the enrollment
*flow* is described in prose). Per section 5.3/manual 3.2: "Voice prints
stored encrypted" — `voice_print` holds ciphertext, never a raw embedding;
encryption is applied by the caller, not this model.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)


class Person(Base):
    __tablename__ = "person"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    graph_node_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph_node.id"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(nullable=False)
    voice_print: Mapped[bytes | None] = mapped_column(nullable=True)  # encrypted ciphertext
    enrolled_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
