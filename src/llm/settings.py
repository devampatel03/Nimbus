# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Module-local settings for `src.llm` (prefix `LLM_`).

Env names match the pre-pivot ones (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
`LLM_TIMEOUT_SECONDS`) plus the spend cap and price table. `LLM_PRICE_TABLE`
is JSON: `{"model-id": {"in_per_mtok_inr": "25", "out_per_mtok_inr": "100"}}`;
the `"*"` entry prices any model not listed (unknown models are never free).

INV-7: `api_key` is a `SecretStr`, and validation errors hide input values.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_API_KEY = (
    "not-needed"  # placeholder for key-less servers; not a secret  # pragma: allowlist secret
)


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    in_per_mtok_inr: Decimal = Field(gt=0)
    out_per_mtok_inr: Decimal = Field(gt=0)


# Deliberately pessimistic placeholder until the bake-off (P5.4) fixes the model.
DEFAULT_PRICE_TABLE: dict[str, ModelPrice] = {
    "*": ModelPrice(in_per_mtok_inr=Decimal(250), out_per_mtok_inr=Decimal(1000)),
}


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
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)

    max_retries: int = Field(default=3, ge=0, le=3)
    backoff_base_s: float = Field(default=0.5, ge=0, le=10)
    backoff_max_s: float = Field(default=8.0, ge=0, le=60)

    daily_spend_cap_inr: Decimal = Field(default=Decimal(200), gt=0)
    price_table: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICE_TABLE))

    # Persist the redacted request text (encrypted) in llm_audit. Off by
    # default: metadata and cost are always stored.
    store_payload: bool = False
    payload_retention_days: int = Field(default=14, ge=1, le=365)

    # Extra canary strings that must never leave the process (INV-7).
    canaries: tuple[str, ...] = ()
