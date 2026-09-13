# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Assistant model (docs/TECHNICAL_ARCHITECTURE.md 7.2): character card
loader, stance log, and friction checker.

PSA-1: the character card is hand-authored and no code path writes it.
`tests/unit/test_agent_character.py` statically verifies this with an AST
scan of `src/`, mirroring the INV-1 egress scan's technique.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)

CHARACTER_CARD_PATH = Path(__file__).resolve().parents[2] / "personality" / "character_card.md"

_GOAL_NEGATION_MARKERS = (
    "stop ",
    "no longer",
    "cancel",
    "abandon",
    "not going to",
    "don't want to",
    "give up on",
)


def load_character_card(path: Path = CHARACTER_CARD_PATH) -> str:
    """PSA-1: read-only. This function must never gain a write mode."""
    if not path.exists():
        raise FileNotFoundError(
            f"character card not found at {path} -- it must be hand-authored, "
            "see docs/TECHNICAL_ARCHITECTURE.md section 7.2"
        )
    return path.read_text(encoding="utf-8")


class AgentStance(Base):
    __tablename__ = "agent_stance"
    __table_args__ = (
        CheckConstraint(
            "strength IN ('tentative', 'held', 'firm')",
            name="ck_agent_stance_strength",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    topic: Mapped[str] = mapped_column(nullable=False)
    position: Mapped[str] = mapped_column(nullable=False)
    reasoning: Mapped[str] = mapped_column(nullable=False)
    taken_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    strength: Mapped[str] = mapped_column(nullable=False)
    revised_from: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_stance.id"), nullable=True
    )
    revision_reason: Mapped[str | None] = mapped_column(nullable=True)


class StanceLog:
    """Append-only: a reversal is a new row linking back via `revised_from`,
    never a mutation of the prior stance (PSA-3 acknowledgement needs the
    old position to still be there to point at).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest_stance(self, topic: str) -> AgentStance | None:
        stmt = (
            select(AgentStance)
            .where(AgentStance.topic == topic)
            .order_by(AgentStance.taken_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get_stances_by_topic(self, topic: str) -> list[AgentStance]:
        stmt = select(AgentStance).where(AgentStance.topic == topic).order_by(AgentStance.taken_at)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def record_stance(
        self,
        *,
        topic: str,
        position: str,
        reasoning: str,
        taken_at: datetime,
        session_id: uuid.UUID,
        strength: str,
        revision_reason: str | None = None,
    ) -> AgentStance:
        """PSA-3: a position that differs from the latest one on this topic
        is a reversal -- detected here by linking `revised_from` to the
        prior row, so callers can tell (`stance.revised_from is not None`)
        without a separate query.
        """
        latest = await self.get_latest_stance(topic)
        revised_from = latest.id if latest is not None and latest.position != position else None

        stance = AgentStance(
            topic=topic,
            position=position,
            reasoning=reasoning,
            taken_at=taken_at,
            session_id=session_id,
            strength=strength,
            revised_from=revised_from,
            revision_reason=revision_reason,
        )
        self._session.add(stance)
        await self._session.flush()
        return stance


@dataclass(frozen=True)
class FrictionConflict:
    kind: str  # "stance" | "goal"
    description: str
    reference: str


def check_friction(
    *,
    request: str,
    user_goals: Iterable[str],
    prior_stances: Iterable[AgentStance],
) -> list[FrictionConflict]:
    """Friction mandate (naming, not refusing): surface a conflict between
    the current request and a held/firm prior stance or a stated goal.
    Deliberately a cheap heuristic, not an LLM call -- it runs in the same
    turn as the request and must not add a blocking network call (INV-4).
    """
    request_lower = request.lower()
    conflicts: list[FrictionConflict] = []

    for stance in prior_stances:
        if stance.strength in ("held", "firm") and stance.topic.lower() in request_lower:
            conflicts.append(
                FrictionConflict(
                    kind="stance",
                    description=f"this touches '{stance.topic}', where I hold: {stance.position}",
                    reference=str(stance.id),
                )
            )

    if any(marker in request_lower for marker in _GOAL_NEGATION_MARKERS):
        for goal in user_goals:
            # Crude stem (first 6 chars) so "exercising" matches goal word
            # "exercise" without pulling in a full NLP dependency for a
            # cheap, on-turn heuristic check.
            goal_stems = [w[:6] for w in goal.lower().split() if len(w) > 3]
            if any(stem in request_lower for stem in goal_stems):
                conflicts.append(
                    FrictionConflict(
                        kind="goal",
                        description=f"this may conflict with your stated goal: {goal}",
                        reference=goal,
                    )
                )

    return conflicts
