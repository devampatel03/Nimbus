# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""In-memory `UpdateStore` double with the same semantics as the Postgres one
(the real one is covered in tests/integration/gateway)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from src.gateway.store import Seen


class MemStore:
    def __init__(
        self,
        max_deliveries: int = 5,
        *,
        poison_min_age_s: float = 0.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.max_deliveries = max_deliveries
        self.poison_min_age_s = poison_min_age_s
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self.received: dict[int, datetime] = {}
        self.notices: dict[int, datetime] = {}
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
            age = (self.clock() - self.received[update_id]).total_seconds()
            if self.deliveries[update_id] > self.max_deliveries and age >= self.poison_min_age_s:
                self.rows[update_id] = True
                self.notices[update_id] = self.received[update_id]
                self._advance()
                return Seen.POISON
            return Seen.REDELIVERY
        self.rows[update_id] = False
        self.received[update_id] = self.clock()
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

    async def pending_poison_notices(self) -> list[tuple[int, datetime]]:
        return sorted(self.notices.items())

    async def clear_poison_notice(self, update_id: int) -> None:
        self.notices.pop(update_id, None)
