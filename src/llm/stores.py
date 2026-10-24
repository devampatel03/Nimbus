# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""In-memory flag / audit / spend-ledger implementations: tests, and the
process-local default used by the legacy `VLLMClient` shim until the app
container injects the Postgres ones (`src.llm.pg_stores`)."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.llm.protocols import AuditRecord


class InMemoryFlagStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        value = self._data.get(key)
        return copy.deepcopy(value) if value is not None else None

    async def set(self, key: str, value: dict[str, Any], *, updated_by: str) -> None:
        self._data[key] = copy.deepcopy(value)


class InMemoryAuditLog:
    """Both the `AuditWriter` and the `SpendLedger` (spend = sum over rows)."""

    def __init__(self) -> None:
        self.rows: list[tuple[uuid.UUID, AuditRecord]] = []

    async def write(self, record: AuditRecord) -> uuid.UUID:
        row_id = uuid.uuid4()
        self.rows.append((row_id, record))
        return row_id

    async def spent_between(self, start: datetime, end: datetime) -> Decimal:
        return sum(
            (r.cost_inr for _, r in self.rows if start <= r.created_at < end),
            Decimal(0),
        )

    @property
    def records(self) -> list[AuditRecord]:
        return [r for _, r in self.rows]
