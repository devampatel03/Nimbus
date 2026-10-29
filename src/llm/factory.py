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

from collections.abc import Sequence
from typing import Any

from src.llm.adapter import OpenAICompatProvider
from src.llm.embeddings_api import API_EMBED_MODEL, ApiEmbedder, OpenAICompatEmbeddingProvider
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
    payload_redact: Sequence[str] = (),
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
        payload_redact=payload_redact,
    )


def build_api_embedder(
    llm: GuardedLLM,
    settings: LLMSettings | None = None,
    *,
    model: str = API_EMBED_MODEL,
    provider: OpenAICompatEmbeddingProvider | None = None,
    transport: Any = None,
    dim: int | None = None,
) -> ApiEmbedder:
    """The API embedder, sharing `llm`'s `CallAccountant` (R5): spend by chat and
    by embeddings is counted against the same in-flight/held totals."""
    resolved = settings if settings is not None else LLMSettings()
    emb_provider = (
        provider
        if provider is not None
        else OpenAICompatEmbeddingProvider(
            OpenAICompatProvider(resolved, transport=transport), model=model
        )
    )
    if dim is None:
        return ApiEmbedder(emb_provider, accountant=llm.accountant, settings=resolved)
    return ApiEmbedder(emb_provider, accountant=llm.accountant, settings=resolved, dim=dim)
