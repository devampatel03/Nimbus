# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Local embedder: lazy load, batching, async, dim/model_id, HF offline (INV-1 part 5).

NEVER loads the real model: a fake SentenceTransformer is injected via
`model_factory`, and a guard fixture fails the test if `sentence_transformers`
gets imported.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from src.memory.embedder import (
    DEFAULT_EMBED_DIM,
    MODEL_NAME,
    EmbedderOfflineError,
    EmbeddingDimensionError,
    EmbeddingMigrationRequired,
    Qwen3Embedder,
    check_index_compat,
    hf_offline_active,
)

DIM = 8


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(k, raising=False)
    yield
    # monkeypatch restores env; embedder load mutates os.environ directly, so reset.
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ.pop(k, None)
    assert "sentence_transformers" not in sys.modules, "real model library was imported"


class FakeST:
    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []
        self.threads: list[int] = []

    def encode(self, sentences: list[str], **kw: Any) -> list[list[float]]:
        assert kw["normalize_embeddings"] is True
        self.calls.append(list(sentences))
        self.threads.append(threading.get_ident())
        return [[float(len(s))] * self.dim for s in sentences]


def make(
    fake: FakeST | None = None, **kw: Any
) -> tuple[Qwen3Embedder, FakeST, list[tuple[str, int]]]:
    kw.setdefault("embed_dim", DIM)
    fake = fake or FakeST(kw["embed_dim"])
    built: list[tuple[str, int]] = []

    def factory(name: str, dim: int) -> FakeST:
        built.append((name, dim))
        return fake

    return Qwen3Embedder(model_factory=factory, **kw), fake, built


def test_model_id_and_default_dim_attributes() -> None:
    e = Qwen3Embedder()
    assert e.model_id == "Qwen/Qwen3-Embedding-0.6B" == MODEL_NAME
    assert e.dim == DEFAULT_EMBED_DIM == 1024
    assert not e.loaded


def test_lazy_load_only_on_first_embed_and_once() -> None:
    e, _, built = make()
    assert built == [] and not e.loaded
    e.embed("a")
    e.embed("b")
    assert built == [(MODEL_NAME, DIM)]


def test_batching_slices_and_preserves_order() -> None:
    e, fake, _ = make(batch_size=2)
    out = e.embed_batch(["a", "bb", "ccc", "dddd", "eeeee"])
    assert [len(c) for c in fake.calls] == [2, 2, 1]
    assert [v[0] for v in out] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert e.embed_batch([]) == []


def test_embed_matches_protocol_shape() -> None:
    e, _, _ = make()
    v = e.embed("hey")
    assert isinstance(v, list) and len(v) == DIM and all(isinstance(x, float) for x in v)


async def test_async_runs_in_worker_thread() -> None:
    e, fake, _ = make()
    out = await e.aembed_batch(["x", "yy"])
    assert len(out) == 2 and fake.threads[0] != threading.get_ident()
    assert (await e.aembed("z"))[0] == 1.0


async def test_concurrent_first_use_loads_once() -> None:
    e, _, built = make()
    await asyncio.gather(*(e.aembed(str(i)) for i in range(8)))
    assert len(built) == 1


def test_wrong_width_output_is_rejected() -> None:
    e, _, _ = make(FakeST(dim=DIM + 1))
    with pytest.raises(EmbeddingDimensionError):
        e.embed("x")


def test_check_index_compat_requires_migration_on_model_or_dim_change() -> None:
    e, _, _ = make()
    check_index_compat(e, stored_model_id=MODEL_NAME, stored_dim=DIM)
    with pytest.raises(EmbeddingMigrationRequired, match="re-embed"):
        check_index_compat(e, stored_model_id=MODEL_NAME, stored_dim=1024)
    with pytest.raises(EmbeddingMigrationRequired):
        check_index_compat(e, stored_model_id="other/model", stored_dim=DIM)


# ---- INV-1 part 5: HF_HUB_OFFLINE -------------------------------------------


def test_loading_forces_hf_offline_before_factory_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")  # a hostile value is overridden
    seen: dict[str, str | None] = {}

    def factory(name: str, dim: int) -> FakeST:
        seen["hub"] = os.environ.get("HF_HUB_OFFLINE")
        seen["tf"] = os.environ.get("TRANSFORMERS_OFFLINE")
        return FakeST()

    Qwen3Embedder(embed_dim=DIM, model_factory=factory).embed("x")
    assert seen == {"hub": "1", "tf": "1"}
    assert hf_offline_active()


def test_inference_fails_closed_if_offline_is_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    e, fake, _ = make()
    e.embed("x")
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    with pytest.raises(EmbedderOfflineError):
        e.embed("y")
    assert len(fake.calls) == 1  # the second call never reached the model


def test_allow_download_is_the_only_way_to_skip_enforcement() -> None:
    e, _, _ = make(allow_download=True)
    e.embed("x")
    assert not hf_offline_active()  # image-build prefetch path leaves env alone


def test_no_network_at_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("network access during embedding")

    e, _, _ = make()
    e.embed("warm")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert len(e.embed_batch(["a", "b"])) == 2
