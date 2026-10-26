# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P2.4 M4: the app lifespan runs `MemoryManager.verify_index()` and refuses to
start when the stored index came from another embedding model/dim."""

import pytest

from src.api.main import create_app, lifespan
from src.memory.embedder import EmbeddingMigrationRequired
from src.memory.manager import DeterministicStubEmbedder
from src.memory.store import PgVectorMemoryStore


async def test_lifespan_refuses_start_on_mixed_index(monkeypatch: pytest.MonkeyPatch) -> None:
    async def other(self: PgVectorMemoryStore) -> set[tuple[str, int]]:
        return {("some-older-model", 1024)}

    monkeypatch.setattr(PgVectorMemoryStore, "stored_models", other)
    app = create_app()
    app.state.memory_embedder = DeterministicStubEmbedder()
    with pytest.raises(EmbeddingMigrationRequired):
        async with lifespan(app):
            pytest.fail("app must not start")


async def test_lifespan_starts_when_index_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    async def same(self: PgVectorMemoryStore) -> set[tuple[str, int]]:
        return {("DeterministicStubEmbedder", 1024)}

    monkeypatch.setattr(PgVectorMemoryStore, "stored_models", same)
    app = create_app()
    app.state.memory_embedder = DeterministicStubEmbedder()
    async with lifespan(app):
        pass
