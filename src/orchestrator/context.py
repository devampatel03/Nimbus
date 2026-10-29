# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Context assembly for one turn (TARGET_DESIGN 4.1). Trusted material (the
character card, traits, stances) goes into the system prompt; everything that
came from memory is rendered only as a delimited DATA block (INV-6). Raw
strings are never accepted as data.

P3.0 (ADR-0010, H18 interim default): a recalled UNTRUSTED item (lane
untrusted, or any source other than plain "memory") goes through the
quarantine lane; without a quarantine, or if quarantine fails, its content is
withheld behind a fixed notice. Either way the turn's taint is set. Trusted
memory is data-framed for defence in depth and does not taint. Quarantine calls
are bounded by the turn's `QuarantineBudget` (P3.0 MED-D); over budget, the
item is withheld (`over_budget`).

P4.4: `memory_canary` (from the `CanaryGuard`) is planted as the first
trusted memory block, so a model induced to dump "what you remember" leaks a
canary that the outbound check recognises.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from src.contracts.memory_query import MemoryQuery
from src.lanes.data import BlockOrigin, DataBlock, SourceKind, UntrustedText
from src.lanes.quarantine import (
    Quarantine,
    QuarantineBudget,
    QuarantineError,
    local_data_block,
    withheld_block,
)
from src.lanes.schemas import SCHEMA_FOR_SOURCE
from src.lanes.taint import Taint, TaintSource
from src.llm.types import ChatMessage
from src.memory.store import ChunkHit, MemoryStore
from src.personality.agent_character import AgentStance
from src.personality.prompt_assembler import assemble_system_prompt
from src.personality.user_profile import UserTrait

log = logging.getLogger(__name__)

MAX_MEMORY_CHARS = 1200


@dataclass(frozen=True)
class RecalledMemory:
    text: str
    source: str = "memory"
    # Code-derived from the memory row's lane (R8/INV-6), never from the model.
    untrusted: bool = False

    @property
    def is_untrusted(self) -> bool:
        """Fail closed: any source other than plain "memory" is untrusted."""
        return self.untrusted or self.source != "memory"


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
            RecalledMemory(
                text=h.content,
                source="memory:untrusted" if h.untrusted else "memory",
                untrusted=h.untrusted,
            )
            for h in hits
        ]


@dataclass(frozen=True)
class TurnContext:
    system_prompt: str
    history: tuple[ChatMessage, ...]
    # Monotonic per-turn taint (ADR-0010); the loop keeps marking it.
    taint: Taint = field(default_factory=Taint)


async def _memory_block(
    m: RecalledMemory,
    quarantine: Quarantine | None,
    taint: Taint,
    turn_id: UUID | None,
    budget: QuarantineBudget,
) -> DataBlock:
    text = str(m.text or "")
    if not m.is_untrusted:
        return local_data_block(
            m.source, text[:MAX_MEMORY_CHARS], origin=BlockOrigin.LOCAL_MEMORY, untrusted=False
        )
    taint.mark(TaintSource.UNTRUSTED_MEMORY)
    label = "memory.untrusted"
    if quarantine is None:
        return withheld_block(label, reason="quarantine_unavailable")
    source = SourceKind.UNTRUSTED_MEMORY
    try:
        out = await quarantine.extract(
            UntrustedText(text, source=source),
            SCHEMA_FOR_SOURCE[source],
            turn_id=turn_id,
            budget=budget,
        )
    except QuarantineError as e:
        return withheld_block(label, reason=e.code)
    taint.mark(TaintSource.QUARANTINE_OUTPUT)
    return out.data_block()


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
    quarantine: Quarantine | None = None,
    turn_id: UUID | None = None,
    budget: QuarantineBudget | None = None,
    memory_canary: str | None = None,
) -> TurnContext:
    blocks: list[DataBlock] = []
    taint = Taint()
    if memory_canary is not None:
        # P4.4: a code-authored trusted memory item at the head of the preamble.
        blocks.append(
            local_data_block(
                "memory", memory_canary, origin=BlockOrigin.LOCAL_MEMORY, untrusted=False
            )
        )
    # P3.0 MED-D: always bounded. The loop passes its per-turn budget (shared
    # with tool output); a direct caller gets the default cap.
    turn_budget = budget if budget is not None else QuarantineBudget()
    if recall is not None:
        try:
            memories = await recall.recall(user_text, limit=memory_limit)
        except Exception:  # noqa: BLE001 - fail closed
            log.warning("memory recall failed; continuing without memory")
            memories = []
        for m in list(memories)[:memory_limit]:
            block = await _memory_block(m, quarantine, taint, turn_id, turn_budget)
            taint.absorb(block)
            blocks.append(block)
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
    return TurnContext(system_prompt=prompt, history=tuple(history), taint=taint)
