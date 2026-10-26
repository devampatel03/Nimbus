# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Memory recall for the turn loop: one short session per call (the loop is
long-lived, a session is not)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.memory.store import PgVectorMemoryStore, VectorEmbedder
from src.orchestrator.context import RecalledMemory, StoreRecall


class SessionScopedRecall:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], embedder: VectorEmbedder
    ) -> None:
        self._sf = session_factory
        self._embedder = embedder

    async def recall(self, query: str, *, limit: int) -> Sequence[RecalledMemory]:
        async with self._sf() as session:
            store = PgVectorMemoryStore(session, embedder=self._embedder)
            return await StoreRecall(store).recall(query, limit=limit)
