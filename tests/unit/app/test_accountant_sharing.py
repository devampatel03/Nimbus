# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""R5: GuardedLLM and the API embedder spend against ONE `CallAccountant`
(P2.3 open MED), and the app wiring uses the shared one."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from src.llm.embeddings_api import ApiEmbedder, OpenAICompatEmbeddingProvider
from src.llm.factory import build_api_embedder
from src.llm.settings import ModelPrice
from src.llm.stores import InMemoryAuditLog, InMemoryFlagStore
from src.llm.types import SpendCapExceeded
from tests.fakes.llm import RecordingTransport, build_env, llm_settings
from tests.unit.test_embedder_api import embedding_response

DIM = 4
PRICE = {"*": ModelPrice(in_per_mtok_inr=Decimal(1000), out_per_mtok_inr=Decimal(1000))}


def _transport() -> RecordingTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        n = len(json.loads(req.content)["input"])
        return embedding_response([[0.5] * DIM for _ in range(n)], tokens=100)

    return RecordingTransport(handler)


def test_factory_embedder_shares_the_guarded_llm_accountant() -> None:
    env = build_env()
    emb = build_api_embedder(env.llm, llm_settings(), transport=_transport(), dim=DIM)
    assert emb._acct is env.llm.accountant
    assert emb._pricing is env.llm.accountant.pricing


async def test_in_flight_chat_reservation_counts_against_embedding_calls() -> None:
    env = build_env(price_table=PRICE, daily_spend_cap_inr=Decimal("0.9"))
    emb = build_api_embedder(
        env.llm, llm_settings(price_table=PRICE), transport=_transport(), dim=DIM
    )
    # A chat call is mid-flight holding 0.8 INR of the 0.9 cap on the shared accountant.
    await env.llm.accountant.reserve([Decimal("0.8")], env.clock())
    # The embedding (est. 0.5 INR) must see it. With a private accountant it would not.
    with pytest.raises(SpendCapExceeded):
        await emb.aembed("x" * 1000)
    assert emb._acct is env.llm.accountant


def test_accountant_and_stores_are_mutually_exclusive() -> None:
    env = build_env()
    prov = OpenAICompatEmbeddingProvider.__new__(OpenAICompatEmbeddingProvider)
    prov.name, prov.model = "p", "m"
    with pytest.raises(ValueError):
        ApiEmbedder(prov, accountant=env.llm.accountant, flags=InMemoryFlagStore())
    with pytest.raises(ValueError):
        ApiEmbedder(prov)  # neither
    audit = InMemoryAuditLog()
    ApiEmbedder(
        prov, flags=InMemoryFlagStore(), audit=audit, ledger=audit
    )  # legacy form still works
