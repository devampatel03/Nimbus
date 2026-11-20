# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""User trait model (docs/TECHNICAL_ARCHITECTURE.md 7.1): SQLAlchemy schema,
`ProfileManager` (load/save/threshold query) and the prompt injector.

PSU-1: a trait without evidence is a hallucination and must not exist --
enforced in `ProfileManager.save_trait`, not left to a nightly cleanup job.
PSU-2: traits below confidence 0.6 are stored but never asserted in prompt
assembly -- enforced in both `get_traits_above_confidence` and
`format_traits_for_prompt`, so a caller holding a raw trait list can't leak
a low-confidence candidate into the prompt by skipping the query filter.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)

DEFAULT_CONFIDENCE_THRESHOLD = 0.6  # PSU-2


class UserTrait(Base):
    __tablename__ = "user_trait"
    __table_args__ = (
        CheckConstraint(
            "user_status IN ('unreviewed', 'approved', 'edited', 'vetoed')",
            name="ck_user_trait_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category: Mapped[str] = mapped_column(nullable=False)
    statement: Mapped[str] = mapped_column(nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    observation_count: Mapped[int] = mapped_column(nullable=False, default=1)
    first_observed: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    last_reinforced: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    user_status: Mapped[str] = mapped_column(nullable=False, default="unreviewed")
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("user_trait.id"), nullable=True
    )


class TraitEvidence(Base):
    """Section 7.1's SQL gives this table no primary key; an ORM row needs
    one, so `id` is added here -- the same kind of minimal, flagged addition
    as `graph_node` (AD-5), not a spec-mandated field.
    """

    __tablename__ = "trait_evidence"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trait_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("user_trait.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    excerpt: Mapped[str] = mapped_column(nullable=False)
    observed_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)


@dataclass(frozen=True)
class EvidenceInput:
    session_id: uuid.UUID
    excerpt: str
    observed_at: datetime


class TraitWithoutEvidenceError(ValueError):
    """PSU-1: a trait may not be created without at least one evidence row."""


class ProfileManager:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_trait(
        self,
        *,
        category: str,
        statement: str,
        confidence: float,
        first_observed: datetime,
        evidence: Iterable[EvidenceInput],
    ) -> UserTrait:
        evidence_list = list(evidence)
        if not evidence_list:
            raise TraitWithoutEvidenceError("PSU-1: a trait must have at least one evidence row")

        trait = UserTrait(
            category=category,
            statement=statement,
            confidence=confidence,
            observation_count=len(evidence_list),
            first_observed=first_observed,
            last_reinforced=max(item.observed_at for item in evidence_list),
        )
        self._session.add(trait)
        await self._session.flush()

        for item in evidence_list:
            self._session.add(
                TraitEvidence(
                    trait_id=trait.id,
                    session_id=item.session_id,
                    excerpt=item.excerpt,
                    observed_at=item.observed_at,
                )
            )
        await self._session.flush()
        return trait

    async def load_trait(self, trait_id: uuid.UUID) -> UserTrait | None:
        return await self._session.get(UserTrait, trait_id)

    async def get_traits_above_confidence(
        self, min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD
    ) -> list[UserTrait]:
        """PSU-2 + veto exclusion: only traits fit to be believed."""
        stmt = select(UserTrait).where(
            UserTrait.confidence >= min_confidence,
            UserTrait.user_status != "vetoed",
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


def format_traits_for_prompt(
    traits: Iterable[UserTrait], min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD
) -> str:
    """The prompt injector: formats a trait slice for system prompt inclusion."""
    lines = [
        f"- [{trait.category}] {trait.statement}"
        for trait in traits
        if trait.confidence >= min_confidence and trait.user_status != "vetoed"
    ]
    if not lines:
        return ""
    return "## What I know about you\n" + "\n".join(lines)
