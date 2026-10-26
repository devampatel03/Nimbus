# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""API embeddings fallback (P2.3, ADR-0013): same `Embedder` interface as the
local `Qwen3Embedder`, used when the VM has < 3 GB headroom for the local model.

Egress and guard path (INV-1, R4, R5):

* This module imports NO HTTP/LLM SDK. The wire call is
  `OpenAICompatProvider.embeddings()`, on the SDK client built from
  `src.net.build_sdk_http_client(Service.LLM)`: host allowlist, deny rules and
  egress-denial logging all apply.
* Accounting is NOT re-implemented here: it is `src.llm.guard.CallAccountant`,
  the same class `GuardedLLM` uses (reserve -> send -> bill/hold -> shielded
  audit -> release; held spend counts in the cap; 4xx unbilled;
  timeout-after-send billed). Pre-send checks, in order: kill switch, size caps
  (`input_too_large`), scrub of ALL texts (secrets/canaries block, card/Aadhaar/
  OTP redacted), spend reservation for the WHOLE input. Only then are
  sub-batches of `MAX_BATCH` sent, so a late secret or an over-cap input
  makes zero provider calls. Each refusal writes a `blocked` row.
* Vectors are validated (count, width, NaN/Inf, zero norm) inside the billed
  send, so a rejected response is billed and audited as `error`.
* No internal retry (a retry is a second billed send and goes back through the
  guard). Fixed-code errors only (INV-7).
* Trade-off: embedded texts leave the machine. That is why this is a fallback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from src.llm.adapter import OpenAICompatProvider
from src.llm.guard import CallAccountant, CallMeta
from src.llm.pricing import PriceTable, estimate_tokens
from src.llm.protocols import AlertHook, AuditWriter, Clock, FlagStore, SpendLedger
from src.llm.scrub import Scrubber, ScrubBlocked
from src.llm.settings import DEFAULT_API_KEY, LLMSettings
from src.llm.types import LLMRefused, Purpose, Usage
from src.memory.embedder import DEFAULT_EMBED_DIM, EmbeddingError, validate_vectors

MAX_BATCH = 64
MAX_TEXT_CHARS = 32_000  # ~8k tokens: the embedding models' context window
MAX_REQUEST_CHARS = 512_000
MAX_REQUEST_ITEMS = 1_024
API_EMBED_MODEL = "text-embedding-3-small"  # placeholder default; override via ctor


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    usage: Usage


class EmbeddingProvider(Protocol):
    name: str
    model: str

    async def embed_batch(self, texts: Sequence[str], *, dim: int) -> EmbeddingBatch: ...


class OpenAICompatEmbeddingProvider:
    """`/embeddings` via the adapter's public `embeddings()`."""

    def __init__(self, provider: OpenAICompatProvider, *, model: str = API_EMBED_MODEL) -> None:
        self._provider = provider
        self.name = provider.name
        self.model = model

    async def embed_batch(self, texts: Sequence[str], *, dim: int) -> EmbeddingBatch:
        vectors, usage = await self._provider.embeddings(self.model, texts, dim=dim)
        return EmbeddingBatch(vectors=vectors, usage=usage)


class ApiEmbedder:
    """Guarded API embedder. `embed`/`embed_batch` (sync) exist for the
    `Embedder` protocol; prefer `aembed`/`aembed_batch`."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        flags: FlagStore | None = None,
        audit: AuditWriter | None = None,
        ledger: SpendLedger | None = None,
        accountant: CallAccountant | None = None,
        settings: LLMSettings | None = None,
        scrubber: Scrubber | None = None,
        pricing: PriceTable | None = None,
        alert: AlertHook | None = None,
        clock: Clock | None = None,
        dim: int = DEFAULT_EMBED_DIM,
    ) -> None:
        self._provider = provider
        self._settings = settings if settings is not None else LLMSettings()
        key = self._settings.api_key.get_secret_value()
        self._scrubber = scrubber or Scrubber(
            canaries=self._settings.canaries,
            secrets=() if key == DEFAULT_API_KEY else (key,),
        )
        if accountant is not None:
            # Shared with GuardedLLM (R5): one set of in-flight/held spend. Its
            # pricing and clock win, so estimates and bills agree.
            if flags is not None or audit is not None or ledger is not None:
                raise ValueError("pass either an accountant or flags/audit/ledger, not both")
            self._acct = accountant
            self._pricing = accountant.pricing
            self._clock: Clock = accountant.clock
        else:
            if flags is None or audit is None or ledger is None:
                raise ValueError("flags, audit and ledger are required without an accountant")
            self._pricing = pricing or PriceTable(self._settings.price_table)
            self._clock = clock or (lambda: datetime.now(UTC))
            self._acct = CallAccountant(
                flags=flags,
                audit=audit,
                ledger=ledger,
                settings=self._settings,
                pricing=self._pricing,
                alert=alert,
                clock=self._clock,
            )
        self.dim = dim
        self.model_id = f"api:{provider.model}"
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ API

    async def aembed(self, text: str) -> list[float]:
        return (await self.aembed_batch([text]))[0]

    async def aembed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self._loop = asyncio.get_running_loop()
        items = list(texts)
        if not items:
            return []
        model = self._provider.model
        meta = CallMeta(Purpose.EMBED.value, self._provider.name, model, self._clock())

        async def checks() -> tuple[list[list[str]], list[Decimal]]:
            await self._acct.check_kill_switch()
            too_big = len(items) > MAX_REQUEST_ITEMS or sum(map(len, items)) > MAX_REQUEST_CHARS
            if too_big or any(len(t) > MAX_TEXT_CHARS for t in items):
                raise LLMRefused("input_too_large")
            try:
                clean = [self._scrubber.scrub(t).text for t in items]  # ALL texts, up front
            except ScrubBlocked:
                raise LLMRefused("scrub_blocked") from None
            slices = [clean[i : i + MAX_BATCH] for i in range(0, len(clean), MAX_BATCH)]
            estimates = [self._pricing.estimate_inr(model, sum(map(len, sl)), 0) for sl in slices]
            return slices, await self._acct.reserve(estimates, meta.started)

        slices, reserved = await self._acct.preflight(meta, checks)
        out: list[list[float]] = []
        for i, sl in enumerate(slices):
            try:
                out.extend(await self._send_slice(meta, sl, reserved[i]))
            except BaseException:
                # Slices never sent are not billed: give their reservation back.
                await self._acct.release_unused(sum(reserved[i + 1 :], Decimal(0)))
                raise
        return out

    async def _send_slice(
        self, meta: CallMeta, texts: list[str], reserved: Decimal
    ) -> list[list[float]]:
        async def send() -> tuple[list[list[float]], Usage]:
            batch = await self._provider.embed_batch(texts, dim=self.dim)
            usage = batch.usage
            if usage.tokens_in == 0:  # provider reported nothing: bill the estimate
                usage = Usage(tokens_in=estimate_tokens(sum(map(len, texts))))
            try:
                validate_vectors(batch.vectors, self.dim, len(texts))
            except EmbeddingError as exc:
                exc.usage = usage  # billed (provider answered) and audited as error
                raise
            return batch.vectors, usage

        async def finish(res: tuple[list[list[float]], Usage]) -> list[list[float]]:
            await self._acct.write_ok(meta, res[1])
            return res[0]

        return await self._acct.billed(meta, reserved, send, finish)

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Sync bridge for `Embedder` callers running in a worker thread
        (`asyncio.to_thread`, as `MemoryManager` does): schedules on the loop
        that last used `aembed*`; with none known it uses a private loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("ApiEmbedder.embed_batch called on a running loop; use aembed_batch")
        loop = self._loop
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(self.aembed_batch(texts), loop).result()
        return asyncio.run(self.aembed_batch(texts))


__all__ = [
    "ApiEmbedder",
    "EmbeddingBatch",
    "EmbeddingProvider",
    "OpenAICompatEmbeddingProvider",
]
