# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Shared helpers for the P2.4 retrieval tests."""

import uuid
from datetime import UTC, datetime
from typing import Any

from src.contracts.event import Event, EventKind
from src.contracts.memory_query import MemoryQuery
from src.memory.manager import DeterministicStubEmbedder
from src.memory.store import PgVectorMemoryStore

EMB = DeterministicStubEmbedder()


def make_event(
    key: str | None = None,
    *,
    lane: str = "trusted",
    occurred_at: datetime | None = None,
    meta: dict[str, Any] | None = None,
    kind: EventKind = EventKind.TEXT,
    confidence: float = 1.0,
) -> Event:
    return Event(
        source_key=key or f"telegram:{uuid.uuid4().int % 10**9}",
        kind=kind,
        source="telegram",
        lane=lane,  # type: ignore[arg-type]
        confidence=confidence,
        body="b",
        meta=meta or {},
        occurred_at=occurred_at or datetime.now(UTC),
    )


def query(text: str, **kw: Any) -> MemoryQuery:
    return MemoryQuery(user_id=uuid.UUID(int=0), query_text=text, top_k=kw.pop("top_k", 10), **kw)


def store_for(session: Any) -> PgVectorMemoryStore:
    return PgVectorMemoryStore(session, embedder=EMB)
