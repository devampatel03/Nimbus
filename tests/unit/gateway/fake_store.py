# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""In-memory `UpdateStore` double with the same semantics as the Postgres one
(the real one is covered in tests/integration/gateway)."""

from __future__ import annotations

import uuid

from src.gateway.store import Seen


class MemStore:
    def __init__(self, max_deliveries: int = 5) -> None:
        self.max_deliveries = max_deliveries
        self.deliveries: dict[int, int] = {}
        self.offset: int | None = None
        self.rows: dict[int, bool] = {}  # update_id -> processed

    async def get_offset(self) -> int | None:
        return self.offset

    async def begin(self, update_id: int, from_user_id: int | None, kind: str) -> Seen:
        if update_id in self.rows:
            if self.rows[update_id]:
                return Seen.DONE
            self.deliveries[update_id] = self.deliveries.get(update_id, 1) + 1
            if self.deliveries[update_id] > self.max_deliveries:
                self.rows[update_id] = True
                self._advance()
                return Seen.POISON
            return Seen.REDELIVERY
        self.rows[update_id] = False
        self.deliveries[update_id] = 1
        return Seen.NEW

    def _advance(self) -> None:
        open_ = [u for u, done in self.rows.items() if not done]
        target = min(open_) if open_ else max(self.rows) + 1
        self.offset = max(self.offset or 0, target)

    async def complete(self, update_id: int, *, event_id: uuid.UUID | None = None) -> None:
        self.rows[update_id] = True
        self._advance()

    async def record_dropped(self, update_id: int, from_user_id: int | None, kind: str) -> None:
        self.rows.setdefault(update_id, True)
        self._advance()
