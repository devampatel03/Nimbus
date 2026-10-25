# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Local sentence-embedding model for episodic memory (P2.3, ADR-0013).

`Qwen3Embedder` wraps `sentence-transformers` around `Qwen/Qwen3-Embedding-0.6B`
(Apache 2.0, see NOTICE) on CPU. Contract:

* Lazy: importing this module, or constructing the embedder, never imports
  `sentence_transformers`, touches the network, or loads weights. The model is
  loaded (once, under a lock) on the first embed call.
* INV-1 part 5: at runtime the embedder makes NO network call. Unless
  constructed with `allow_download=True` (image-build prefetch only), loading
  forces `HF_HUB_OFFLINE=1` (and `TRANSFORMERS_OFFLINE=1`) BEFORE
  `sentence_transformers`/`huggingface_hub` are imported, and every encode
  re-asserts it; if something flipped it off, the call fails closed with
  `EmbedderOfflineError`. Weights must already be on disk (image build).
* Batch + async: `embed_batch` encodes in `batch_size` slices; `aembed` /
  `aembed_batch` run the CPU-bound forward pass in `asyncio.to_thread`.
  The sync `embed(text)` keeps the `Embedder` protocol of `src.memory.manager`.
* `model_id` is an explicit attribute (recorded per row in `memory_chunk`);
  `dim` is the output dimension (default `DEFAULT_EMBED_DIM`, native 1024,
  Matryoshka-truncatable). Output of the wrong width is rejected. Changing the
  dimension or model requires a migration + re-embed job: use
  `check_index_compat` to refuse mixing vectors from different models/dims.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import threading
from collections.abc import Callable, Sequence
from typing import Any, Protocol

MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_EMBED_DIM = 1024
DEFAULT_BATCH_SIZE = 16
_OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")


class EmbeddingError(Exception):
    """Base class. Messages are fixed codes (no input text, INV-7).

    `usage` may be attached by the API path (provider-reported tokens) so the
    shared billing accountant bills the real cost of a rejected response."""

    usage: Any = None


class EmbedderOfflineError(EmbeddingError):
    """HF offline mode is not in force at inference time (INV-1 part 5)."""


class EmbeddingDimensionError(EmbeddingError):
    """A vector's width differs from the embedder's declared dimension."""


class EmbeddingValueError(EmbeddingError):
    """A vector contains NaN/Inf or has zero norm (unusable for cosine search)."""


class EmbeddingMigrationRequired(EmbeddingError):
    """Stored vectors came from a different model/dimension: migrate + re-embed."""


class BatchEmbedder(Protocol):
    """What both embedders offer on top of the `Embedder` protocol."""

    model_id: str
    dim: int

    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...
    async def aembed(self, text: str) -> list[float]: ...
    async def aembed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...


def check_index_compat(embedder: BatchEmbedder, *, stored_model_id: str, stored_dim: int) -> None:
    """Refuse to query/insert against an index built by another model or width."""
    if embedder.model_id != stored_model_id or embedder.dim != stored_dim:
        raise EmbeddingMigrationRequired(
            f"embedder {embedder.model_id}/{embedder.dim} != stored "
            f"{stored_model_id}/{stored_dim}: schema migration and re-embed job required"
        )


def validate_vectors(vectors: Sequence[Sequence[float]], dim: int, expected: int) -> None:
    """Reject wrong count/width (`EmbeddingDimensionError`) and NaN/Inf or
    zero-norm vectors (`EmbeddingValueError`): they poison cosine ranking."""
    if len(vectors) != expected:
        raise EmbeddingDimensionError(f"expected {expected} vectors, got {len(vectors)}")
    for v in vectors:
        if len(v) != dim:
            raise EmbeddingDimensionError(f"expected {dim} dims, got {len(v)}")
        if not all(math.isfinite(x) for x in v):
            raise EmbeddingValueError("vector contains NaN or Inf")
        if not any(x != 0.0 for x in v):
            raise EmbeddingValueError("vector has zero norm")


def hf_offline_active() -> bool:
    """True iff HF offline mode is on in the environment AND, if
    `huggingface_hub` is already imported, in its cached constant."""
    if any(os.environ.get(k) != "1" for k in _OFFLINE_ENV):
        return False
    consts = sys.modules.get("huggingface_hub.constants")
    return not (consts is not None and getattr(consts, "HF_HUB_OFFLINE", True) is not True)


def enforce_hf_offline() -> None:
    for k in _OFFLINE_ENV:
        os.environ[k] = "1"
    consts: Any = sys.modules.get("huggingface_hub.constants")
    if consts is not None:  # imported earlier: it cached the old env value
        consts.HF_HUB_OFFLINE = True


ModelFactory = Callable[[str, int], Any]


def _default_factory(model_name: str, dim: int) -> Any:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, device="cpu", truncate_dim=dim)


class Qwen3Embedder:
    """Local CPU embedder implementing the `Embedder` protocol (+ batch/async)."""

    def __init__(
        self,
        *,
        model_name: str = MODEL_NAME,
        embed_dim: int = DEFAULT_EMBED_DIM,
        batch_size: int = DEFAULT_BATCH_SIZE,
        allow_download: bool = False,
        model_factory: ModelFactory | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.model_id = model_name
        self.dim = embed_dim
        self._batch_size = batch_size
        self._allow_download = allow_download
        self._factory: ModelFactory = model_factory or _default_factory
        self._model: Any = None
        self._lock = threading.Lock()

    # Back-compat with the pre-P2.3 private names.
    @property
    def _model_name(self) -> str:
        return self.model_id

    @property
    def _embed_dim(self) -> int:
        return self.dim

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _assert_offline(self) -> None:
        if not self._allow_download and not hf_offline_active():
            raise EmbedderOfflineError("HF_HUB_OFFLINE=1 is required at inference (INV-1)")

    def _load(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    if not self._allow_download:
                        enforce_hf_offline()  # before sentence_transformers is imported
                    self._model = self._factory(self.model_id, self.dim)
        return self._model

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        self._assert_offline()
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            part = list(texts[i : i + self._batch_size])
            raw = model.encode(
                part,
                batch_size=self._batch_size,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            out.extend([float(x) for x in row] for row in raw)
        validate_vectors(out, self.dim, len(texts))
        return out

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    async def aembed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_batch, list(texts))

    async def aembed(self, text: str) -> list[float]:
        return (await self.aembed_batch([text]))[0]
