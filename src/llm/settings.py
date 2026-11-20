# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Module-local settings for `src.llm` (prefix `LLM_`).

Env names match the pre-pivot ones (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
`LLM_TIMEOUT_SECONDS`) plus the spend cap and price table. `LLM_PRICE_TABLE`
is JSON: `{"model-id": {"in_per_mtok_inr": "25", "out_per_mtok_inr": "100"}}`;
the `"*"` entry prices any model not listed (unknown models are never free).

P5.5 (ADR-0025), provider-agnostic and cost-minimal:

- `LLM_BASE_URL` is any OpenAI-compatible https endpoint; its exact host becomes
  the one `Service.LLM` host in `src.net` (default-deny, no wildcard).
- `LLM_PRICE_IN_PER_MTOK_INR` / `LLM_PRICE_OUT_PER_MTOK_INR` price `LLM_MODEL`
  without writing JSON; `LLM_CHEAP_PRICE_*` price `LLM_MODEL_CHEAP`. A price
  of 0 is rejected: a free-tier key is priced at a tiny non-zero value (e.g.
  `0.0001`) and bounded by the request limits instead.
- `LLM_MODEL_CHEAP` (optional) serves the quarantine and extraction purposes;
  chat keeps `LLM_MODEL`. Unset => everything uses `LLM_MODEL`.
- Free-tier resilience: `LLM_RPM_LIMIT`, `LLM_DAILY_REQUEST_LIMIT` (IST day),
  `LLM_BACKGROUND_SHARE` (background purposes may use at most this share of
  both, so chat always has headroom) and `LLM_RATE_LIMIT_MAX_WAIT_S` (how long
  a chat call may wait for a slot or a 429 Retry-After before the fixed
  "busy" reply).
- Per-lane output caps (`LLM_MAX_OUTPUT_TOKENS_*`) clamp every request in the
  guard, whatever the caller asked for.

INV-7: `api_key` is a `SecretStr`, and validation errors hide input values.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)

DEFAULT_API_KEY = (
    "not-needed"  # placeholder for key-less servers; not a secret  # pragma: allowlist secret
)

# `cost.hard_daily_cap_inr` in quality/thresholds.yaml. The setting cannot exceed it.
MAX_DAILY_SPEND_CAP_INR = Decimal(100)
# Below this INR/Mtok (input or output) a model is treated as free-tier priced:
# the INR cap is then not a real bound, the request limits are (ADR-0025).
FREE_TIER_PRICE_INR = Decimal("0.01")


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    in_per_mtok_inr: Decimal = Field(gt=0)
    out_per_mtok_inr: Decimal = Field(gt=0)


# Deliberately pessimistic placeholder until the bake-off (P5.4) fixes the model.
DEFAULT_PRICE_TABLE: dict[str, ModelPrice] = {
    "*": ModelPrice(in_per_mtok_inr=Decimal(250), out_per_mtok_inr=Decimal(1000)),
}

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    base_url: str = "http://localhost:8000/v1"
    api_key: SecretStr = SecretStr(DEFAULT_API_KEY)
    model: str = "Qwen/Qwen2.5-7B-Instruct"
    # Optional cheaper model for QUARANTINE_EXTRACT / MEMORY_EXTRACT / FRICTION.
    model_cheap: str | None = None
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    # Sent as `reasoning_effort` when set (thinking tokens bill as output).
    reasoning_effort: ReasoningEffort | None = None

    max_retries: int = Field(default=3, ge=0, le=3)
    backoff_base_s: float = Field(default=0.5, ge=0, le=10)
    backoff_max_s: float = Field(default=8.0, ge=0, le=60)

    # Bounded by cost.hard_daily_cap_inr (100); default 50 = 1,500 INR / 30 days,
    # the cost.projected_monthly_inr_max threshold.
    daily_spend_cap_inr: Decimal = Field(default=Decimal(50), gt=0, le=MAX_DAILY_SPEND_CAP_INR)
    price_table: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICE_TABLE))
    price_in_per_mtok_inr: Decimal | None = Field(default=None, gt=0)
    price_out_per_mtok_inr: Decimal | None = Field(default=None, gt=0)
    cheap_price_in_per_mtok_inr: Decimal | None = Field(default=None, gt=0)
    cheap_price_out_per_mtok_inr: Decimal | None = Field(default=None, gt=0)

    # Request limits (free tier). Counted in-process; see src/llm/ratelimit.py.
    # Paid-key defaults (generous); a free key sets these below its quota (COST_MODEL.md s5).
    rpm_limit: int = Field(default=120, ge=1, le=10_000)
    daily_request_limit: int = Field(default=2000, ge=1, le=1_000_000)
    background_share: float = Field(default=0.5, gt=0, le=1)
    rate_limit_max_wait_s: float = Field(default=40.0, ge=0, le=120)

    # Per-lane output caps, applied by the guard to every request.
    max_output_tokens_trusted: int = Field(default=600, ge=16, le=8000)
    max_output_tokens_quarantine: int = Field(default=800, ge=16, le=8000)
    max_output_tokens_system: int = Field(default=400, ge=16, le=8000)

    # Persist the redacted request text (encrypted) in llm_audit. Off by
    # default: metadata and cost are always stored.
    store_payload: bool = False
    payload_retention_days: int = Field(default=14, ge=1, le=365)

    # Extra canary strings that must never leave the process (INV-7).
    canaries: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _merge_env_prices(self) -> Self:
        pairs = (
            (self.model, self.price_in_per_mtok_inr, self.price_out_per_mtok_inr),
            (self.model_cheap, self.cheap_price_in_per_mtok_inr, self.cheap_price_out_per_mtok_inr),
        )
        table = dict(self.price_table)
        for model, p_in, p_out in pairs:
            if (p_in is None) != (p_out is None):
                raise ValueError("set both the input and the output price, or neither")
            if p_in is None or p_out is None:
                continue
            if not model:
                raise ValueError("a cheap-model price needs LLM_MODEL_CHEAP")
            table[model] = ModelPrice(in_per_mtok_inr=p_in, out_per_mtok_inr=p_out)
        object.__setattr__(self, "price_table", table)
        return self

    def free_tier_priced_models(self) -> tuple[str, ...]:
        """The configured models (`LLM_MODEL`, `LLM_MODEL_CHEAP`) whose effective
        price is below `FREE_TIER_PRICE_INR` per Mtok, input or output (a
        free-tier key priced at e.g. 0.0001). For them the INR cap bounds
        nothing; only the request limits do."""
        out: list[str] = []
        for model in dict.fromkeys(m for m in (self.model, self.model_cheap) if m):
            p = self.price_table.get(model) or self.price_table.get("*") or DEFAULT_PRICE_TABLE["*"]
            if min(p.in_per_mtok_inr, p.out_per_mtok_inr) < FREE_TIER_PRICE_INR:
                out.append(model)
        return tuple(out)

    def output_cap(self, lane: str) -> int:
        """The output-token ceiling for a `Lane` value."""
        if lane == "trusted":
            return self.max_output_tokens_trusted
        if lane == "quarantine":
            return self.max_output_tokens_quarantine
        return self.max_output_tokens_system


def warn_if_free_tier_pricing(settings: LLMSettings) -> bool:
    """Log one startup WARNING when a configured model is free-tier priced.
    Fixed text plus the numeric limits only: no key, URL or model name."""
    count = len(settings.free_tier_priced_models())
    if count:
        _log.warning(
            "LLM price below %s INR/Mtok for %d configured model(s): free-tier pricing, "
            "the daily INR spend cap is NOT a real bound; only the request limits apply "
            "(rpm=%d daily=%d background_share=%s)",
            FREE_TIER_PRICE_INR,
            count,
            settings.rpm_limit,
            settings.daily_request_limit,
            settings.background_share,
        )
    return count > 0
