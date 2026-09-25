# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Assistant model (docs/TECHNICAL_ARCHITECTURE.md 7.2): character card
loader, stance log, and friction checker.

PSA-1: the character card is hand-authored and no code path writes it.
`tests/unit/test_agent_character.py` statically verifies this with an AST
scan of `src/`, mirroring the INV-1 egress scan's technique.

`check_friction` (sync, keyword heuristic) is kept UNCHANGED from the
earlier session (this session does not touch it): `tests/eval/personality/
friction.py` -- protected by CLAUDE.md rule 4 ("never modify files in
tests/eval/ without explicit permission") -- calls it synchronously and
is graded against this exact function; making it async or changing its
behavior would silently break that already-passing, real-measurement
harness. `check_friction_llm` below is the new LLM-based conflict
detector this session's task asks for, added alongside it (not wired into
`friction.py`, which stays on the original heuristic) -- "an addition,
not a replacement of tested interfaces", same resolution as
`src/orchestrator/approver.py`'s `review`/`review_async` split.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.llm.client import ChatMessage, LLMClient, VLLMClient
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


_FRICTION_SYSTEM_PROMPT = (
    "You check whether a new user request genuinely conflicts with the "
    "assistant's prior held/firm stances or the user's own stated goals. "
    "Given the request, the user's goals, and the assistant's prior "
    "stances, list any real conflicts -- be conservative, only flag a "
    "genuine tension, not a loose keyword overlap. Respond only via the "
    "given JSON schema; return an empty conflicts list if there is no real "
    "conflict."
)


class _FrictionConflictOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str  # "stance" | "goal"
    description: str
    reference: str


class _FrictionJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conflicts: list[_FrictionConflictOut] = Field(default_factory=list)


async def check_friction_llm(
    *,
    request: str,
    user_goals: Iterable[str],
    prior_stances: Iterable[AgentStance],
    llm_client: LLMClient | None = None,
) -> list[FrictionConflict]:
    """LLM-based friction check (this session) -- see module docstring for why
    this is an addition alongside `check_friction`, not a replacement of
    it. Same scoped inputs as the heuristic (request, goals, prior
    stances only; no full conversation history), still "fast" in the
    sense of being a single local-model call, not a cloud round trip --
    but unlike `check_friction`, this genuinely does block on a network
    call, so callers on a latency-sensitive path should prefer the sync
    heuristic until this is benchmarked against a running local model.
    """
    client = llm_client or VLLMClient()
    stances_text = (
        "\n".join(
            f"- topic={s.topic!r} position={s.position!r} strength={s.strength}"
            for s in prior_stances
        )
        or "(none)"
    )
    goals_text = "\n".join(f"- {g}" for g in user_goals) or "(none)"
    messages: list[ChatMessage] = [
        {"role": "system", "content": _FRICTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"request: {request}\n\nuser goals:\n{goals_text}\n\n"
                f"assistant's prior stances:\n{stances_text}"
            ),
        },
    ]
    judgment = await client.structured(messages, schema=_FrictionJudgment)
    return [
        FrictionConflict(kind=c.kind, description=c.description, reference=c.reference)
        for c in judgment.conflicts
    ]
