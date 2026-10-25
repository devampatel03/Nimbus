# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Price table and cost arithmetic (R5). All amounts are INR `Decimal`."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_UP, Decimal

from src.llm.settings import DEFAULT_PRICE_TABLE, ModelPrice

_MILLION = Decimal(1_000_000)
_QUANT = Decimal("0.0001")  # llm_audit.cost_inr is Numeric(12, 4)

# Conservative chars-per-token for the pre-call estimate: real tokenisers give
# 3-4 chars/token for English, fewer for Indic scripts; 2 over-estimates.
_CHARS_PER_TOKEN = 2


class PriceTable:
    def __init__(self, prices: Mapping[str, ModelPrice] | None = None) -> None:
        table = dict(prices if prices is not None else DEFAULT_PRICE_TABLE)
        if "*" not in table:
            # Fail safe: an unlisted model must still cost something.
            table["*"] = DEFAULT_PRICE_TABLE["*"]
        self._prices = table

    def price_for(self, model: str) -> ModelPrice:
        return self._prices.get(model, self._prices["*"])

    def cost_inr(self, model: str, tokens_in: int, tokens_out: int) -> Decimal:
        p = self.price_for(model)
        raw = (p.in_per_mtok_inr * tokens_in + p.out_per_mtok_inr * tokens_out) / _MILLION
        return raw.quantize(_QUANT, rounding=ROUND_UP)

    def estimate_inr(self, model: str, input_chars: int, max_output_tokens: int) -> Decimal:
        """Upper-bound estimate used by the spend-cap pre-check."""
        est_in = -(-input_chars // _CHARS_PER_TOKEN)
        return self.cost_inr(model, est_in, max_output_tokens)


def estimate_tokens(chars: int) -> int:
    return -(-chars // _CHARS_PER_TOKEN)
