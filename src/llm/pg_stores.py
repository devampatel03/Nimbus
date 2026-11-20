# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Postgres `FlagStore` / `AuditWriter` / `SpendLedger` over `system_flag` and
`llm_audit` (src/memory/schemas/ops.py). `llm_audit` is append-only (DB
trigger); the redacted payload is stored only when a cipher is supplied,
encrypted, with a retention deadline (INV-7)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.llm.protocols import AuditRecord, PayloadEncryptor
from src.memory.schemas.ops import LLMAudit, SystemFlag

_PAYLOAD_AAD = b"llm_audit.payload_redacted"


class PostgresFlagStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(SystemFlag, key)
            return dict(row.value) if row is not None else None

    async def set(self, key: str, value: dict[str, Any], *, updated_by: str) -> None:
        stmt = pg_insert(SystemFlag).values(key=key, value=value, updated_by=updated_by)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SystemFlag.key],
            set_={"value": value, "updated_by": updated_by, "updated_at": func.now()},
        )
        async with self._sf() as session, session.begin():
            await session.execute(stmt)


class PostgresAuditStore:
    """`AuditWriter` + `SpendLedger` over `llm_audit`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cipher: PayloadEncryptor | None = None,
        retention_days: int = 14,
    ) -> None:
        self._sf = session_factory
        self._cipher = cipher
        self._retention = timedelta(days=retention_days)

    async def write(self, record: AuditRecord) -> uuid.UUID:
        enc: bytes | None = None
        expires: datetime | None = None
        if record.payload_redacted is not None and self._cipher is not None:
            enc = self._cipher.encrypt(record.payload_redacted, aad=_PAYLOAD_AAD)
            expires = record.created_at + self._retention
        row = LLMAudit(
            id=uuid.uuid4(),
            created_at=record.created_at,
            purpose=record.purpose,
            provider=record.provider,
            model=record.model,
            status=record.status,
            blocked_reason=record.blocked_reason,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            cost_inr=record.cost_inr,
            latency_ms=record.latency_ms,
            turn_id=record.turn_id,
            payload_redacted_enc=enc,
            payload_expires_at=expires,
        )
        async with self._sf() as session, session.begin():
            session.add(row)
        return row.id

    async def spent_between(self, start: datetime, end: datetime) -> Decimal:
        stmt = select(func.coalesce(func.sum(LLMAudit.cost_inr), 0)).where(
            LLMAudit.created_at >= start, LLMAudit.created_at < end
        )
        async with self._sf() as session:
            total = (await session.execute(stmt)).scalar_one()
        return Decimal(total)
