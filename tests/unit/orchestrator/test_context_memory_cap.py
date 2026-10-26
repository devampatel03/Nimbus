# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P2.5a follow-ups: `build_context` must not trust a recall backend to honour
`limit`, and each recalled memory is capped at MAX_MEMORY_CHARS."""

from __future__ import annotations

from src.orchestrator.context import MAX_MEMORY_CHARS, RecalledMemory, build_context


class _Recall:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    async def recall(self, query: str, *, limit: int) -> list[RecalledMemory]:
        return [RecalledMemory(t) for t in self._texts]  # ignores limit on purpose


async def test_memories_beyond_limit_are_dropped() -> None:
    texts = [f"memory-marker-{i}" for i in range(10)]
    ctx = await build_context(
        character_card="card",
        user_text="q",
        recall=_Recall(texts),
        memory_limit=3,
        nonce_factory=lambda: "n0nce",
    )
    assert [t for t in texts if t in ctx.system_prompt] == texts[:3]


async def test_each_memory_is_capped_at_max_chars() -> None:
    tail = "TAIL-SENTINEL"
    text = "a" * MAX_MEMORY_CHARS + tail
    ctx = await build_context(
        character_card="card",
        user_text="q",
        recall=_Recall([text]),
        nonce_factory=lambda: "n0nce",
    )
    assert "a" * MAX_MEMORY_CHARS in ctx.system_prompt
    assert "a" * (MAX_MEMORY_CHARS + 1) not in ctx.system_prompt
    assert tail not in ctx.system_prompt
