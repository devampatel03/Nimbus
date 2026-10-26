# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Context assembly for one turn (TARGET_DESIGN 4.1). Trusted material (the
character card, traits, stances) goes into the system prompt; everything that
came from memory is untrusted by default and is rendered only as a delimited
DATA block (INV-6). Raw strings are never accepted as data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from src.contracts.memory_query import MemoryQuery
from src.llm.types import ChatMessage
from src.memory.store import ChunkHit, MemoryStore
from src.personality.agent_character import AgentStance
from src.personality.prompt_assembler import DataBlock, assemble_system_prompt
from src.personality.user_profile import UserTrait

log = logging.getLogger(__name__)

MAX_MEMORY_CHARS = 1200


@dataclass(frozen=True)
class RecalledMemory:
    text: str
    source: str = "memory"


class MemoryRecall(Protocol):
    async def recall(self, query: str, *, limit: int) -> Sequence[RecalledMemory]: ...


class StoreRecall:
    """`MemoryRecall` over the pgvector `MemoryStore` (which embeds the query).

    Default search excludes untrusted/unpromoted rows (R8). Every recalled hit
    is rendered as a DATA block regardless; untrusted ones get a distinct
    source label so the model and the audit trail can tell them apart (INV-6).
    """

    def __init__(self, store: MemoryStore, *, user_id: UUID | None = None) -> None:
        self._store = store
        self._user_id = user_id or UUID(int=0)  # single user (AD-8)

    async def recall(self, query: str, *, limit: int) -> Sequence[RecalledMemory]:
        q = MemoryQuery(user_id=self._user_id, query_text=query, top_k=limit)
        hits: list[ChunkHit] = await self._store.search(q)
        return [
            RecalledMemory(text=h.content, source="memory:untrusted" if h.untrusted else "memory")
            for h in hits
        ]


@dataclass(frozen=True)
class TurnContext:
    system_prompt: str
    history: tuple[ChatMessage, ...]


async def build_context(
    *,
    character_card: str,
    user_text: str,
    traits: Iterable[UserTrait] = (),
    stances: Iterable[AgentStance] = (),
    recall: MemoryRecall | None = None,
    history: Sequence[ChatMessage] = (),
    memory_limit: int = 5,
    nonce_factory: Callable[[], str] | None = None,
) -> TurnContext:
    blocks: list[DataBlock] = []
    if recall is not None:
        try:
            memories = await recall.recall(user_text, limit=memory_limit)
        except Exception:  # noqa: BLE001 - fail closed
            log.warning("memory recall failed; continuing without memory")
            memories = []
        blocks = [DataBlock(m.source, str(m.text or "")[:MAX_MEMORY_CHARS]) for m in memories]
    if nonce_factory is None:
        prompt = assemble_system_prompt(
            character_card=character_card, traits=traits, stances=stances, data_blocks=blocks
        )
    else:
        prompt = assemble_system_prompt(
            character_card=character_card,
            traits=traits,
            stances=stances,
            data_blocks=blocks,
            nonce_factory=nonce_factory,
        )
    return TurnContext(system_prompt=prompt, history=tuple(history))
