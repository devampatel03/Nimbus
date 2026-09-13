# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Unit tests for `src/personality/prompt_assembler.py`. Pure function, no
external dependencies needed.
"""

import uuid
from datetime import UTC, datetime

from src.personality.agent_character import AgentStance
from src.personality.prompt_assembler import assemble_system_prompt
from src.personality.user_profile import UserTrait


def _trait(confidence: float, status: str = "approved") -> UserTrait:
    return UserTrait(
        id=uuid.uuid4(),
        category="communication",
        statement="prefers being told the conclusion first",
        confidence=confidence,
        observation_count=3,
        first_observed=datetime.now(UTC),
        last_reinforced=datetime.now(UTC),
        user_status=status,
    )


def _stance() -> AgentStance:
    return AgentStance(
        id=uuid.uuid4(),
        topic="remote work",
        position="hybrid is better",
        reasoning="team cohesion",
        taken_at=datetime.now(UTC),
        session_id=uuid.uuid4(),
        strength="held",
    )


def test_character_card_always_included():
    prompt = assemble_system_prompt(
        character_card="You are Nimbus, dry and direct.",
        traits=[],
        stances=[],
    )

    assert "You are Nimbus, dry and direct." in prompt


def test_low_confidence_traits_excluded():
    prompt = assemble_system_prompt(
        character_card="You are Nimbus.",
        traits=[_trait(0.9), _trait(0.4)],
        stances=[],
    )

    assert prompt.count("prefers being told the conclusion first") == 1


def test_vetoed_trait_excluded_even_above_threshold():
    prompt = assemble_system_prompt(
        character_card="You are Nimbus.",
        traits=[_trait(0.95, status="vetoed")],
        stances=[],
    )

    assert "prefers being told the conclusion first" not in prompt


def test_stances_included():
    prompt = assemble_system_prompt(
        character_card="You are Nimbus.",
        traits=[],
        stances=[_stance()],
    )

    assert "hybrid is better" in prompt


def test_active_context_included_when_present():
    prompt = assemble_system_prompt(
        character_card="You are Nimbus.",
        traits=[],
        stances=[],
        active_context="user is mid-conversation about trip planning",
    )

    assert "trip planning" in prompt


def test_no_empty_sections_when_nothing_to_add():
    prompt = assemble_system_prompt(character_card="You are Nimbus.", traits=[], stances=[])

    assert prompt == "You are Nimbus."
