# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Assembles the per-turn system prompt from `AgentState` (personality
slice + retrieved/warm memory + conversation history), delegating the
actual composition to `src.personality.prompt_assembler.assemble_system_prompt`
rather than duplicating its section-ordering logic.

Pure, no I/O of its own except the one local (non-network) file read in
`load_character_card` -- reading a hand-authored file off local disk does
not block on a network call, so this does not weaken INV-4. Everything
else here is string formatting over data already sitting in `AgentState`.

`UserProfileSlice.traits`/`.active_stances` are the frozen contract's
`TraitSummary`/`StanceSummary` shapes; `prompt_assembler` expects the
personality module's own ORM-backed `UserTrait`/`AgentStance`. These are
bridged with transient (never-persisted, never-flushed) instances built
only from the fields `format_traits_for_prompt`/`_format_stances` actually
read -- constructing them this way does not implicitly satisfy those
classes' DB-level `nullable=False` constraints, which is fine since these
objects are never added to a session or flushed.
"""

from collections.abc import Iterable
from pathlib import Path

from src.contracts.memory_query import MemoryResult
from src.contracts.user_profile_slice import UserProfileSlice
from src.orchestrator.state import AgentState, ConversationTurn
from src.personality.agent_character import CHARACTER_CARD_PATH, AgentStance, load_character_card
from src.personality.prompt_assembler import assemble_system_prompt
from src.personality.user_profile import UserTrait


def _traits_from_slice(slice_: UserProfileSlice | None) -> list[UserTrait]:
    if slice_ is None:
        return []
    return [
        UserTrait(
            category=t.category,
            statement=t.statement,
            confidence=t.confidence,
            user_status="approved",
        )
        for t in slice_.traits
    ]


def _stances_from_slice(slice_: UserProfileSlice | None) -> list[AgentStance]:
    if slice_ is None:
        return []
    return [
        AgentStance(
            topic=s.topic,
            position=s.position,
            strength=s.strength,
            reasoning="",
        )
        for s in slice_.active_stances
    ]


def _format_memories(memories: Iterable[MemoryResult]) -> str:
    lines = [f"- {m.content}" for m in memories]
    if not lines:
        return ""
    return "## Relevant memory\n" + "\n".join(lines)


def _format_history(history: Iterable[ConversationTurn]) -> str:
    lines = [f"{turn.role}: {turn.content}" for turn in history]
    if not lines:
        return ""
    return "## Recent conversation\n" + "\n".join(lines)


def build_active_context(state: AgentState) -> str:
    blocks = [
        _format_history(state.conversation_history),
        _format_memories(state.retrieved_memories),
    ]
    return "\n\n".join(b for b in blocks if b)


def build_system_prompt(
    state: AgentState, *, character_card_path: Path = CHARACTER_CARD_PATH
) -> str:
    character_card = load_character_card(character_card_path)
    return assemble_system_prompt(
        character_card=character_card,
        traits=_traits_from_slice(state.personality_slice),
        stances=_stances_from_slice(state.personality_slice),
        active_context=build_active_context(state),
    )
