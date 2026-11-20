# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""R4/R5 regression + adversarial tests for the API embedder. These mirror the
P1.4b guard tests (tests/unit/llm/test_llm_spend_r5.py, test_llm_guard_r4.py)
and pin that `ApiEmbedder` uses the SAME `CallAccountant` as `GuardedLLM`."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import math
from decimal import Decimal
from typing import Any

import httpx
import pytest

from src.llm.adapter import OpenAICompatProvider
from src.llm.embeddings_api import (
    MAX_BATCH,
    MAX_TEXT_CHARS,
    ApiEmbedder,
    OpenAICompatEmbeddingProvider,
)
from src.llm.guard import CallAccountant, GuardedLLM
from src.llm.settings import ModelPrice
from src.llm.stores import InMemoryAuditLog, InMemoryFlagStore
from src.llm.types import (
    LLMAuditError,
    LLMProviderError,
    LLMRefused,
    SpendCapExceeded,
)
from src.memory.embedder import EmbeddingDimensionError, EmbeddingValueError
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
from tests.unit.test_embedder_api import embedding_response

DIM = 4
TEST_TIMEOUT_S = 30
# 1000 INR / Mtok on input: a 1000-char text estimates 500 tokens = 0.5 INR.
PRICE = {"*": ModelPrice(in_per_mtok_inr=Decimal(1000), out_per_mtok_inr=Decimal(1000))}
TEXT = "x" * 1000


class Env:
    def __init__(self, transport: RecordingTransport, **settings: Any) -> None:
        settings.setdefault("price_table", PRICE)
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
            dim=DIM,
        )


def good(tokens: int = 500) -> RecordingTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        n = len(json.loads(req.content)["input"])
        return embedding_response([[0.5] * DIM for _ in range(n)], tokens=tokens)

    return RecordingTransport(handler)


async def drive(env: Env, n: int = 10) -> list[str]:
    out: list[str] = []
    for _ in range(n):
        try:
            await env.emb.aembed(TEXT)
            out.append("ok")
        except SpendCapExceeded:
            out.append("cap")
        except LLMAuditError:
            out.append("audit_err")
        except LLMProviderError:
            out.append("prov_err")
        except Exception:  # noqa: BLE001
            out.append("other_err")
    return out


def test_shares_the_one_accountant_implementation() -> None:
    env = Env(good())
    assert type(env.emb._acct) is CallAccountant
    s = llm_settings()
    g = GuardedLLM(
        OpenAICompatProvider(s), flags=env.flags, audit=env.audit, ledger=env.audit, settings=s
    )
    assert type(g._acct) is CallAccountant
    # No private re-implementation of the R5 machinery in the embedder module.
    import src.llm.embeddings_api as mod

    assert not hasattr(mod.ApiEmbedder, "_check_spend")
    assert not hasattr(mod.ApiEmbedder, "_audit_safely")


# ---- HIGH-1: audit-write failure holds the reservation ------------------------


async def test_ok_audit_write_failure_holds_reservation_and_cap_trips() -> None:
    t = good()
    env = Env(t, daily_spend_cap_inr="1.0")
    real = env.audit.write

    async def write(rec: Any) -> Any:
        if rec.status == "ok":
            raise OSError("disk full")
        return await real(rec)

    env.audit.write = write  # type: ignore[method-assign]
    outcomes = await drive(env, 10)
    assert "cap" in outcomes
    assert len(t.calls) <= 2  # 1.0 cap / 0.5 per send; the PoC got 50/50 through


# ---- HIGH-2: cancel during the ok-row write -----------------------------------


async def test_cancel_during_ok_audit_write_holds_reservation() -> None:
    t = good()
    env = Env(t, daily_spend_cap_inr="0.6")
    gate = asyncio.Event()
    entered = asyncio.Event()
    real = env.audit.write

    async def slow(rec: Any) -> Any:
        if rec.status == "ok":
            entered.set()
            await gate.wait()
        return await real(rec)

    env.audit.write = slow  # type: ignore[method-assign]
    task = asyncio.create_task(env.emb.aembed(TEXT))
    # Deterministic sync (not a yield count): the first send lazily builds the
    # SDK client and runs thread-offloaded setup, which takes a variable number
    # of loop turns on a cold process.
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert len(t.calls) == 1  # parked inside the ok-row write
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    # The send happened and was never confirmed in the ledger: still counted.
    with pytest.raises(SpendCapExceeded):
        await env.emb.aembed(TEXT)
    gate.set()
    for _ in range(200):
        if any(r.status == "ok" for r in env.audit.records):
            break
        await asyncio.sleep(0.01)
    assert [r.status for r in env.audit.records].count("ok") == 1  # shielded write finished


# ---- MED-1: sent classification ------------------------------------------------


@pytest.mark.parametrize(
    ("make_resp", "billed"),
    [
        (lambda r: httpx.Response(400, json={"error": "bad"}), False),
        (lambda r: httpx.Response(401, json={"error": "bad"}), False),
        (lambda r: httpx.Response(422, json={"error": "bad"}), False),
        (lambda r: httpx.Response(504, json={"error": "gw"}), True),
        (lambda r: httpx.Response(408, json={"error": "to"}), True),
    ],
)
async def test_status_classification_matches_guard(make_resp: Any, billed: bool) -> None:
    t = RecordingTransport(make_resp)
    env = Env(t, daily_spend_cap_inr="1.0")
    outcomes = await drive(env, 6)
    if billed:
        assert "cap" in outcomes and len(t.calls) <= 2
        assert all(r.cost_inr > 0 for r in env.audit.records if r.status == "error")
    else:
        assert "cap" not in outcomes and len(t.calls) == 6
        assert all(r.cost_inr == 0 for r in env.audit.records)


async def test_timeout_after_send_is_billed_and_trips_cap() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    t = RecordingTransport(handler)
    env = Env(t, daily_spend_cap_inr="1.0")
    outcomes = await drive(env, 8)
    assert "cap" in outcomes and len(t.calls) <= 2
    assert all(r.cost_inr > 0 for r in env.audit.records if r.status == "error")


# ---- MED-2: whole-input scrub + reservation before the first send --------------


async def test_late_secret_in_batch_refused_with_zero_provider_calls() -> None:
    t = good(tokens=1)
    env = Env(t)
    texts = [f"item {i}" for i in range(100)]
    texts[70] = "leak " + API_KEY
    with pytest.raises(LLMRefused) as ei:
        await env.emb.aembed_batch(texts)
    assert ei.value.reason == "scrub_blocked" and t.calls == []
    assert env.audit.records[-1].status == "blocked"


async def test_whole_input_cap_checked_before_first_send() -> None:
    t = good(tokens=1)
    env = Env(t, daily_spend_cap_inr="1.0")
    # 130 texts => 3 sub-batches; the total estimate (> 1.0) must refuse up front.
    texts = ["y" * 1000] * (MAX_BATCH * 2 + 2)
    with pytest.raises(SpendCapExceeded):
        await env.emb.aembed_batch(texts)
    assert t.calls == []


async def test_failure_in_later_sub_batch_releases_unsent_reservations() -> None:
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 2:
            return httpx.Response(400, json={"error": "bad"})
        n = len(json.loads(req.content)["input"])
        return embedding_response([[0.5] * DIM for _ in range(n)], tokens=1)

    env = Env(RecordingTransport(handler), daily_spend_cap_inr="100")
    with pytest.raises(LLMProviderError):
        await env.emb.aembed_batch(["z" * 100] * (MAX_BATCH * 3))
    assert env.emb._acct._inflight == 0  # the 3rd slice's reservation was given back


# ---- MED-3: vector validity ----------------------------------------------------


@pytest.mark.parametrize(
    ("vec", "exc"),
    [
        ([math.nan, 1.0, 1.0, 1.0], EmbeddingValueError),
        ([math.inf, 1.0, 1.0, 1.0], EmbeddingValueError),
        ([-math.inf, 1.0, 1.0, 1.0], EmbeddingValueError),
        ([0.0, 0.0, 0.0, 0.0], EmbeddingValueError),
        ([1.0, 1.0, 1.0], EmbeddingDimensionError),
    ],
)
async def test_bad_vectors_rejected_billed_and_audited_as_error(vec: list[float], exc: Any) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        # NaN/Inf are not valid JSON numbers; send them as the provider might.
        body = json.dumps(
            {
                "object": "list",
                "model": "m",
                "data": [{"object": "embedding", "index": 0, "embedding": vec}],
                "usage": {"prompt_tokens": 40, "total_tokens": 40},
            }
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        )

    env = Env(RecordingTransport(handler))
    with pytest.raises(exc):
        await env.emb.aembed("hello")
    rec = env.audit.records[-1]
    assert rec.status == "error" and rec.tokens_in == 40 and rec.cost_inr > 0


def test_local_validate_vectors_rejects_bad_values() -> None:
    from src.memory.embedder import validate_vectors

    validate_vectors([[1.0, 0.0]], 2, 1)
    for bad in ([math.nan, 1.0], [math.inf, 0.0], [0.0, 0.0]):
        with pytest.raises(EmbeddingValueError):
            validate_vectors([bad], 2, 1)


# ---- MED-4: size caps before reserving spend -----------------------------------


@pytest.mark.parametrize(
    "texts",
    [
        ["a" * (MAX_TEXT_CHARS + 1)],
        ["a" * 2_000_000],
        ["a" * 30_000] * 40,  # each text fine, request total over cap
        ["a"] * 5_000,  # too many items
    ],
)
async def test_oversize_inputs_refused_before_reserving_spend(texts: list[str]) -> None:
    t = good()
    env = Env(t, daily_spend_cap_inr="1.0")
    with pytest.raises(LLMRefused) as ei:
        await env.emb.aembed_batch(texts)
    assert ei.value.reason == "input_too_large" and t.calls == []
    assert env.emb._acct._inflight == 0
    assert await env.flags.get("llm_spend_mode") is None  # did not trip the cap
    assert env.audit.records[-1].status == "blocked"


# ---- misc: pre-send refusals are never billed -----------------------------------


async def test_pre_send_blocks_are_not_billed() -> None:
    t = good()
    env = Env(t)
    await env.flags.set("llm_kill_switch", {"engaged": True}, updated_by="t")
    with pytest.raises(LLMRefused):
        await env.emb.aembed(TEXT)
    assert t.calls == [] and all(r.cost_inr == 0 for r in env.audit.records)


async def test_guard_bug_in_preflight_fails_closed() -> None:
    t = good()
    env = Env(t)

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("store down")

    env.flags.get = boom  # type: ignore[method-assign]
    with pytest.raises(LLMRefused) as ei:
        await env.emb.aembed(TEXT)
    assert ei.value.reason == "guard_error" and t.calls == []


def _bounded(fn: Any) -> Any:
    """Hard per-test timeout: a regression must fail, never hang CI."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=TEST_TIMEOUT_S)

    return wrapper


for _name, _obj in list(globals().items()):
    if _name.startswith("test_") and inspect.iscoroutinefunction(_obj):
        globals()[_name] = _bounded(_obj)
