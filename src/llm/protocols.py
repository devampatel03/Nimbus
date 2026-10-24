# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Protocols of the `llm` module. Injected stores keep the guard testable
without Postgres; Postgres implementations live in `src.llm.stores`."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from src.llm.types import ChatRequest, ChatResult, LLMMode, ProviderResponse


class LLMClient(Protocol):
    """The only path to a model (INV-1, R4). See `GuardedLLM` for the order of
    guard checks. Fails closed: a guard error raises `LLMRefused` and makes no
    provider call."""

    async def chat(self, req: ChatRequest) -> ChatResult: ...

    async def mode(self) -> LLMMode: ...


class Provider(Protocol):
    """Wire-level completion. Implemented by `OpenAICompatProvider` and the
    fakes. Never called directly outside `GuardedLLM`."""

    name: str
    model: str

    async def complete(self, req: ChatRequest) -> ProviderResponse: ...


class FlagStore(Protocol):
    """`system_flag` rows (kill switch, spend mode)."""

    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], *, updated_by: str) -> None: ...


@dataclass(frozen=True)
class AuditRecord:
    """Shape of one `llm_audit` row (migration b7d2e9f40a15). `payload_redacted`
    is the scrubbed request text; it is only ever persisted encrypted."""

    purpose: str
    provider: str
    model: str
    status: str  # ok | blocked | error
    created_at: datetime
    blocked_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_inr: Decimal = Decimal(0)
    latency_ms: int | None = None
    turn_id: uuid.UUID | None = None
    payload_redacted: str | None = None


class AuditWriter(Protocol):
    async def write(self, record: AuditRecord) -> uuid.UUID: ...


class SpendLedger(Protocol):
    async def spent_between(self, start: datetime, end: datetime) -> Decimal:
        """Sum of `cost_inr` for rows with start <= created_at < end (UTC)."""
        ...


class PayloadEncryptor(Protocol):
    """Satisfied by `src.db.crypto.DataCipher`."""

    def encrypt(self, plaintext: bytes | str, *, aad: bytes = b"") -> bytes: ...


AlertHook = Callable[[str], Awaitable[None]]
Clock = Callable[[], datetime]
