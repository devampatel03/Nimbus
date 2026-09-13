# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""GET /memories -- debug/review surface over `MemoryManager.query`
(hybrid episodic + semantic retrieval, `src/memory/manager.py`).

Not a frozen contract, not INV-4-constrained: this is an operator debug
tool, not a Mode A turn, so it is fine for it to do the full
Qdrant+Postgres round trip that `get_warm_context`/Mode A deliberately
avoid.

`user_id` defaults to the nil UUID: per AD-8, Nimbus is single-user and
self-hosted, so one instance holds one user's data and there is no real
per-user identity to look up. The query param exists for forwards
compatibility (e.g. a future multi-profile mode), not because it does
anything today -- `MemoryQuery.user_id` is carried through `MemoryResult`
for provenance shape parity, not used to filter (AD-8).
"""

from uuid import UUID

from fastapi import APIRouter, Query

from src.contracts.memory_query import MemoryQuery, MemoryResult
from src.db import postgres, qdrant, redis
from src.memory.manager import MemoryManager

_NIL_USER_ID = UUID(int=0)

router = APIRouter()


@router.get("", response_model=list[MemoryResult])
async def list_memories(
    q: str = Query(..., description="search query text"),
    top_k: int = Query(10, ge=1, le=100),
    user_id: UUID = Query(_NIL_USER_ID),
) -> list[MemoryResult]:
    async with postgres.get_session() as session:
        manager = MemoryManager(
            pg_session=session, qdrant=qdrant.qdrant_client, redis=redis.redis_client
        )
        return await manager.query(q, MemoryQuery(user_id=user_id, query_text=q, top_k=top_k))
