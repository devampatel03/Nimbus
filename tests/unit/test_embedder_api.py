# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""API embeddings fallback: same Embedder interface, behind the R4 guard path.
Wire tests use the real adapter on a mock transport (no network)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from src.llm.adapter import OpenAICompatProvider
from src.llm.embeddings_api import (
    ApiEmbedder,
    EmbeddingBatch,
    OpenAICompatEmbeddingProvider,
)
from src.llm.guard import KILL_SWITCH_KEY
from src.llm.scrub import Scrubber
from src.llm.stores import InMemoryAuditLog, InMemoryFlagStore
from src.llm.types import LLMAuditError, LLMProviderError, LLMRefused, SpendCapExceeded, Usage
from src.memory.embedder import EmbeddingDimensionError
from src.net import NetSettings
from tests.fakes.llm import (
    API_KEY,
    LLM_URL,
    Alerts,
    FakeClock,
    RecordingTransport,
    llm_settings,
    no_sleep,
)

DIM = 4


def embedding_response(
    vectors: list[list[float]], tokens: int = 10, order: list[int] | None = None
) -> httpx.Response:
    idx = order or list(range(len(vectors)))
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "emb-m",
            "data": [{"object": "embedding", "index": i, "embedding": vectors[i]} for i in idx],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        },
    )


class Env:
    def __init__(self, transport: RecordingTransport, dim: int = DIM, **settings: object) -> None:
        s = llm_settings(**settings)
        self.flags = InMemoryFlagStore()
        self.audit = InMemoryAuditLog()
        self.alerts = Alerts()
        self.transport = transport
        base = OpenAICompatProvider(
            s, net_settings=NetSettings(llm_base_url=LLM_URL), transport=transport, sleep=no_sleep
        )
        self.emb = ApiEmbedder(
            OpenAICompatEmbeddingProvider(base, model="emb-m"),
            flags=self.flags,
            audit=self.audit,
            ledger=self.audit,
            settings=s,
            alert=self.alerts,
            clock=FakeClock(),
            dim=dim,
        )


def ok_transport(dim: int = DIM) -> RecordingTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        n = len(json.loads(req.content)["input"])
        return embedding_response([[0.1 * (i + 1)] * dim for i in range(n)], tokens=5 * n)

    return RecordingTransport(handler)


async def test_wire_shape_goes_through_adapter_and_net() -> None:
    env = Env(ok_transport())
    out = await env.emb.aembed_batch(["alpha", "beta"])
    assert len(out) == 2 and all(len(v) == DIM for v in out)
    call = env.transport.calls[0]
    assert call.url.host == "llm.example.com" and call.url.path.endswith("/embeddings")
    assert call.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(call.content)
    assert body["model"] == "emb-m" and body["input"] == ["alpha", "beta"]
    assert body["dimensions"] == DIM and body["encoding_format"] == "float"


async def test_results_reordered_by_index_and_audited_with_cost() -> None:
    t = RecordingTransport(
        lambda r: embedding_response([[1.0] * DIM, [2.0] * DIM], tokens=40, order=[1, 0])
    )
    env = Env(t)
    out = await env.emb.aembed_batch(["a", "b"])
    assert [v[0] for v in out] == [1.0, 2.0]
    (rec,) = env.audit.records
    assert (rec.purpose, rec.status, rec.tokens_in) == ("embed", "ok", 40)
    assert rec.cost_inr > Decimal(0) and rec.payload_redacted is None


async def test_batches_larger_than_cap_are_split() -> None:
    env = Env(ok_transport())
    out = await env.emb.aembed_batch([f"t{i}" for i in range(130)])
    assert len(out) == 130 and len(env.transport.calls) == 3


async def test_kill_switch_makes_zero_transport_calls() -> None:
    env = Env(ok_transport())
    await env.flags.set(KILL_SWITCH_KEY, {"engaged": True}, updated_by="t")
    with pytest.raises(LLMRefused) as ei:
        await env.emb.aembed("x")
    assert ei.value.reason == "kill_switch" and env.transport.calls == []
    assert env.audit.records[-1].status == "blocked"


async def test_secret_in_text_blocks_without_transport_call() -> None:
    env = Env(ok_transport())
    with pytest.raises(LLMRefused) as ei:
        await env.emb.aembed("my key is " + API_KEY)
    assert ei.value.reason == "scrub_blocked" and env.transport.calls == []


async def test_card_number_is_redacted_before_send() -> None:
    env = Env(ok_transport())
    await env.emb.aembed("pay 4111 1111 1111 1111 now")
    sent = json.loads(env.transport.calls[0].content)["input"][0]
    assert "4111" not in sent


async def test_spend_cap_refuses_flips_read_only_alerts_once() -> None:
    env = Env(ok_transport(), daily_spend_cap_inr="0.0001")
    for _ in range(2):
        with pytest.raises(SpendCapExceeded):
            await env.emb.aembed("x" * 4000)
    assert env.transport.calls == [] and len(env.alerts.messages) == 1


async def test_spend_is_recorded_in_shared_ledger() -> None:
    env = Env(ok_transport())
    await env.emb.aembed("hello")
    spent = await env.audit.spent_between(
        datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    )
    assert spent > 0


async def test_dimension_mismatch_rejected_and_cost_still_recorded() -> None:
    env = Env(ok_transport(dim=DIM + 2))
    with pytest.raises(EmbeddingDimensionError):
        await env.emb.aembed("x")
    assert env.audit.records[-1].status == "error" and env.audit.records[-1].cost_inr > 0


async def test_provider_error_is_fixed_code_without_leaks() -> None:
    t = RecordingTransport(lambda r: httpx.Response(401, json={"error": {"message": API_KEY}}))
    env = Env(t)
    with pytest.raises(LLMProviderError) as ei:
        await env.emb.aembed("x")
    assert API_KEY not in str(ei.value) and ei.value.status == 401
    assert env.audit.records[-1].status == "error"


async def test_egress_denied_for_unlisted_host() -> None:
    s = llm_settings()
    base = OpenAICompatProvider(
        s,
        net_settings=NetSettings(llm_base_url="https://other.example.com/v1"),
        transport=ok_transport(),
        sleep=no_sleep,
    )
    audit = InMemoryAuditLog()
    emb = ApiEmbedder(
        OpenAICompatEmbeddingProvider(base, model="m"),
        flags=InMemoryFlagStore(),
        audit=audit,
        ledger=audit,
        settings=s,
        dim=DIM,
    )
    with pytest.raises(LLMProviderError) as ei:
        await emb.aembed("x")
    assert ei.value.code == "egress_denied"
    assert audit.records[-1].cost_inr == 0


async def test_audit_failure_withholds_result() -> None:
    env = Env(ok_transport())

    async def bad(record: object) -> None:
        raise RuntimeError("db down")

    env.audit.write = bad  # type: ignore[method-assign]
    with pytest.raises(LLMAuditError):
        await env.emb.aembed("x")


def test_model_id_and_dim_attributes() -> None:
    env = Env(ok_transport(), dim=DIM)
    assert env.emb.model_id == "api:emb-m" and env.emb.dim == DIM


async def test_sync_embed_from_worker_thread_uses_captured_loop() -> None:
    env = Env(ok_transport())
    await env.emb.aembed("prime")  # captures the running loop
    v = await asyncio.to_thread(env.emb.embed, "hi")  # how MemoryManager calls it
    assert len(v) == DIM


async def test_sync_embed_on_running_loop_is_refused() -> None:
    env = Env(ok_transport())
    with pytest.raises(RuntimeError):
        env.emb.embed("x")


class StubProvider:
    name = "stub"
    model = "stub-m"

    def __init__(self) -> None:
        self.texts: list[Sequence[str]] = []

    async def embed_batch(self, texts: Sequence[str], *, dim: int) -> EmbeddingBatch:
        self.texts.append(texts)
        return EmbeddingBatch([[1.0] * dim for _ in texts], Usage(tokens_in=3))


async def test_works_with_any_embedding_provider_and_custom_scrubber() -> None:
    p = StubProvider()
    audit = InMemoryAuditLog()
    e = ApiEmbedder(
        p,
        flags=InMemoryFlagStore(),
        audit=audit,
        ledger=audit,
        settings=llm_settings(),
        scrubber=Scrubber(canaries=["CANARY-STRING-1"]),
        dim=2,
    )
    assert await e.aembed("fine") == [1.0, 1.0]
    with pytest.raises(LLMRefused):
        await e.aembed("has CANARY-STRING-1 inside")
    assert len(p.texts) == 1
