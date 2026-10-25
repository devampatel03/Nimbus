# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""API embeddings fallback (P2.3, ADR-0013): same `Embedder` interface as the
local `Qwen3Embedder`, used when the VM has < 3 GB headroom for the local model.

Egress and guard path (INV-1, R4):

* This module imports NO HTTP/LLM SDK. The wire call goes through
  `OpenAICompatProvider`'s SDK client, which is built on
  `src.net.build_sdk_http_client(Service.LLM)`, so the host allowlist and deny
  rules apply to every request.
* `ApiEmbedder` applies the same pre-call guard order as `GuardedLLM`, with
  zero provider calls on any refusal: kill switch -> scrub (secrets/canaries
  block, card/Aadhaar/OTP redacted) -> daily spend cap (IST day, flips the
  shared READ_ONLY flag + alert once) -> provider call -> audit row
  (`purpose=embed`) + cost. If the audit row cannot be written, the result is
  withheld (fail closed). Provider failures are fixed-code `LLMProviderError`s
  (INV-7); there is no internal retry (a caller retry goes through the guard
  again so each send is reserved and billed).
* Trade-off: the texts embedded here leave the machine (memory content goes to
  the embedding provider). That is the reason this is a fallback, not the default.

Known gap (follow-up): `OpenAICompatProvider` has no public `embeddings` method;
`OpenAICompatEmbeddingProvider` uses its lazily-built SDK client (`_sdk()`).
Adding a public method to `adapter.py` (not owned by P2.3) would remove that.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from src.llm.adapter import OpenAICompatProvider, _egress_error
from src.llm.guard import KILL_SWITCH_KEY, SPEND_MODE_KEY, ist_day_bounds
from src.llm.pricing import PriceTable, estimate_tokens
from src.llm.protocols import AlertHook, AuditRecord, AuditWriter, Clock, FlagStore, SpendLedger
from src.llm.scrub import Scrubber, ScrubBlocked
from src.llm.settings import DEFAULT_API_KEY, LLMSettings
from src.llm.types import (
    LLMAuditError,
    LLMProviderError,
    LLMRefused,
    LLMResponseError,
    Purpose,
    SpendCapExceeded,
    Usage,
)
from src.memory.embedder import DEFAULT_EMBED_DIM, EmbeddingDimensionError, validate_vectors
from src.net import EgressDenied

logger = logging.getLogger("nimbus.llm")

MAX_BATCH = 64
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
    """`/embeddings` over the adapter's egress-bound SDK client."""

    def __init__(self, provider: OpenAICompatProvider, *, model: str = API_EMBED_MODEL) -> None:
        self._provider = provider
        self.name = provider.name
        self.model = model

    async def embed_batch(self, texts: Sequence[str], *, dim: int) -> EmbeddingBatch:
        try:
            raw = await self._provider._sdk().embeddings.create(
                model=self.model,
                input=list(texts),
                dimensions=dim,
                encoding_format="float",
            )
        except LLMProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, never re-raised raw (INV-7)
            egress = _egress_error(exc)
            if egress is not None:
                code = "egress_denied" if isinstance(egress, EgressDenied) else "egress_failed"
                raise LLMProviderError(code) from None
            status = getattr(exc, "status_code", None)
            raise LLMProviderError(
                "provider_status" if isinstance(status, int) else "unexpected",
                status=status if isinstance(status, int) else None,
            ) from None
        try:
            data = sorted(raw.data, key=lambda d: d.index)
            vectors = [[float(x) for x in d.embedding] for d in data]
            usage = Usage(tokens_in=int(raw.usage.prompt_tokens or 0)) if raw.usage else Usage()
        except Exception:  # noqa: BLE001
            raise LLMResponseError("malformed_response") from None
        return EmbeddingBatch(vectors=vectors, usage=usage)


class ApiEmbedder:
    """Guarded API embedder. `embed`/`embed_batch` (sync) exist for the
    `Embedder` protocol; prefer `aembed`/`aembed_batch`."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        flags: FlagStore,
        audit: AuditWriter,
        ledger: SpendLedger,
        settings: LLMSettings | None = None,
        scrubber: Scrubber | None = None,
        pricing: PriceTable | None = None,
        alert: AlertHook | None = None,
        clock: Clock | None = None,
        dim: int = DEFAULT_EMBED_DIM,
    ) -> None:
        self._provider = provider
        self._flags = flags
        self._audit = audit
        self._ledger = ledger
        self._settings = settings if settings is not None else LLMSettings()
        key = self._settings.api_key.get_secret_value()
        self._scrubber = scrubber or Scrubber(
            canaries=self._settings.canaries,
            secrets=() if key == DEFAULT_API_KEY else (key,),
        )
        self._pricing = pricing or PriceTable(self._settings.price_table)
        self._alert = alert
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self.dim = dim
        self.model_id = f"api:{provider.model}"
        self._inflight = Decimal(0)
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ API

    async def aembed(self, text: str) -> list[float]:
        return (await self.aembed_batch([text]))[0]

    async def aembed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self._loop = asyncio.get_running_loop()
        out: list[list[float]] = []
        for i in range(0, len(texts), MAX_BATCH):
            out.extend(await self._guarded_batch(list(texts[i : i + MAX_BATCH])))
        return out

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

    # ------------------------------------------------------------ the guard

    async def _guarded_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        started = self._clock()
        model = self._provider.model
        reserved = Decimal(0)
        try:
            try:
                await self._check_kill_switch()
                clean = self._scrub(texts)
                reserved = await self._check_spend(clean, model, started)
            except LLMRefused as exc:
                await self._audit_safely(model, started, "blocked", exc.reason)
                raise
            except Exception:  # noqa: BLE001 - any guard bug fails closed
                logger.error("embedding guard error; refusing call")
                await self._audit_safely(model, started, "blocked", "guard_error")
                raise LLMRefused("guard_error") from None

            try:
                batch = await self._provider.embed_batch(clean, dim=self.dim)
            except asyncio.CancelledError:
                await asyncio.shield(self._audit_safely(model, started, "error", None, reserved))
                raise
            except LLMProviderError as exc:
                not_sent = exc.code in {"egress_denied", "egress_failed", "egress_config"}
                await asyncio.shield(
                    self._audit_safely(
                        model, started, "error", None, Decimal(0) if not_sent else reserved
                    )
                )
                raise
            except Exception:
                await asyncio.shield(self._audit_safely(model, started, "error", None, reserved))
                raise

            usage = batch.usage
            if usage.tokens_in == 0:
                usage = Usage(tokens_in=estimate_tokens(sum(len(t) for t in clean)))
            cost = self._pricing.cost_inr(model, usage.tokens_in, 0)
            try:
                validate_vectors(batch.vectors, self.dim, len(clean))
            except EmbeddingDimensionError:
                # Billed but unusable: record the cost, reject the result.
                await asyncio.shield(self._audit_safely(model, started, "error", None, cost))
                raise
            now = self._clock()
            try:
                await self._audit.write(
                    AuditRecord(
                        purpose=Purpose.EMBED.value,
                        provider=self._provider.name,
                        model=model,
                        status="ok",
                        created_at=now,
                        tokens_in=usage.tokens_in,
                        cost_inr=cost,
                        latency_ms=max(0, int((now - started).total_seconds() * 1000)),
                    )
                )
            except Exception:  # noqa: BLE001
                logger.error("embedding audit write failed; withholding result")
                raise LLMAuditError from None
            return batch.vectors
        finally:
            if reserved:
                async with self._lock:
                    self._inflight -= reserved

    async def _check_kill_switch(self) -> None:
        try:
            flag = await self._flags.get(KILL_SWITCH_KEY)
        except Exception:  # noqa: BLE001 - unreadable switch => engaged
            raise LLMRefused("guard_error") from None
        if flag is not None and flag.get("engaged") is not False:
            raise LLMRefused("kill_switch")

    def _scrub(self, texts: list[str]) -> list[str]:
        try:
            return [self._scrubber.scrub(t).text for t in texts]
        except ScrubBlocked:
            raise LLMRefused("scrub_blocked") from None

    async def _check_spend(self, texts: list[str], model: str, now: datetime) -> Decimal:
        day, start, end = ist_day_bounds(now)
        cap = self._settings.daily_spend_cap_inr
        estimate = self._pricing.estimate_inr(model, sum(len(t) for t in texts), 0)
        async with self._lock:
            flag = await self._flags.get(SPEND_MODE_KEY)
            if flag and flag.get("mode") == "read_only" and flag.get("day") == day:
                raise SpendCapExceeded
            spent = await self._ledger.spent_between(start, end)
            if spent + self._inflight + estimate > cap:
                await self._flags.set(
                    SPEND_MODE_KEY,
                    {
                        "mode": "read_only",
                        "day": day,
                        "spent_inr": str(spent),
                        "cap_inr": str(cap),
                    },
                    updated_by="llm_guard",
                )
                logger.warning("llm daily spend cap reached; mode=read_only day=%s", day)
                if self._alert is not None:
                    try:
                        await self._alert(
                            f"LLM daily spend cap reached (INR {spent} of {cap}). "
                            "LLM calls are paused until midnight IST; reminders keep firing."
                        )
                    except Exception:  # noqa: BLE001
                        logger.error("llm spend-cap alert hook failed")
                raise SpendCapExceeded
            self._inflight += estimate
        return estimate

    async def _audit_safely(
        self,
        model: str,
        started: datetime,
        status: str,
        reason: str | None,
        cost: Decimal = Decimal(0),
    ) -> None:
        now = self._clock()
        try:
            await self._audit.write(
                AuditRecord(
                    purpose=Purpose.EMBED.value,
                    provider=self._provider.name,
                    model=model,
                    status=status,
                    created_at=now,
                    blocked_reason=reason,
                    cost_inr=cost,
                    latency_ms=max(0, int((now - started).total_seconds() * 1000)),
                )
            )
        except Exception:  # noqa: BLE001 - never mask the original failure
            logger.error("embedding audit write failed for status=%s", status)


__all__ = [
    "ApiEmbedder",
    "EmbeddingBatch",
    "EmbeddingProvider",
    "OpenAICompatEmbeddingProvider",
]
