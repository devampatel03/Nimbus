# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Wiring helpers for `GuardedLLM`.

`build_guarded_llm` is what the app container (later) calls with Postgres
stores. `default_stores()` are PROCESS-LOCAL in-memory stores, shared by every
legacy `VLLMClient()` in the process so that they at least agree with each
other on the kill switch and the daily cap. They are not durable: the app
container must inject `PostgresFlagStore` / `PostgresAuditStore`.
"""

from __future__ import annotations

from typing import Any

from src.llm.adapter import OpenAICompatProvider
from src.llm.guard import GuardedLLM
from src.llm.protocols import AlertHook, AuditWriter, Clock, FlagStore, Provider, SpendLedger
from src.llm.settings import LLMSettings
from src.llm.stores import InMemoryAuditLog, InMemoryFlagStore

_DEFAULT_FLAGS = InMemoryFlagStore()
_DEFAULT_AUDIT = InMemoryAuditLog()


def default_stores() -> tuple[FlagStore, AuditWriter, SpendLedger]:
    return _DEFAULT_FLAGS, _DEFAULT_AUDIT, _DEFAULT_AUDIT


def build_guarded_llm(
    settings: LLMSettings | None = None,
    *,
    provider: Provider | None = None,
    flags: FlagStore | None = None,
    audit: AuditWriter | None = None,
    ledger: SpendLedger | None = None,
    alert: AlertHook | None = None,
    clock: Clock | None = None,
    transport: Any = None,
) -> GuardedLLM:
    resolved = settings if settings is not None else LLMSettings()
    d_flags, d_audit, d_ledger = default_stores()
    return GuardedLLM(
        provider if provider is not None else OpenAICompatProvider(resolved, transport=transport),
        flags=flags if flags is not None else d_flags,
        audit=audit if audit is not None else d_audit,
        ledger=ledger if ledger is not None else d_ledger,
        settings=resolved,
        alert=alert,
        clock=clock,
    )
