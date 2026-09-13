# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""System prompt builder (docs/TECHNICAL_ARCHITECTURE.md section 7): combines
the hand-authored character card with user traits above the PSU-2 confidence
threshold, relevant agent stances, and the active-context slice already
computed for this turn.

Pure function, no I/O of its own -- callers supply already-loaded traits,
stances and context, so this module cannot add a blocking call to the Mode A
response path (INV-4).
"""

from collections.abc import Iterable

from src.personality.agent_character import AgentStance
from src.personality.user_profile import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    UserTrait,
    format_traits_for_prompt,
)


def _format_stances(stances: Iterable[AgentStance]) -> str:
    lines = [f"- On {stance.topic}: {stance.position} ({stance.strength})" for stance in stances]
    if not lines:
        return ""
    return "## My prior positions\n" + "\n".join(lines)


def assemble_system_prompt(
    *,
    character_card: str,
    traits: Iterable[UserTrait],
    stances: Iterable[AgentStance],
    active_context: str = "",
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> str:
    sections = [character_card.strip()]

    trait_block = format_traits_for_prompt(traits, min_confidence=min_confidence)
    if trait_block:
        sections.append(trait_block)

    stance_block = _format_stances(stances)
    if stance_block:
        sections.append(stance_block)

    if active_context.strip():
        sections.append("## Active context\n" + active_context.strip())

    return "\n\n".join(sections)
