# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`LLM_*` env names are unchanged; the key is a secret."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.llm.settings import LLMSettings


def test_env_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-env-value-0123456789")
    monkeypatch.setenv("LLM_MODEL", "m-1")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("LLM_DAILY_SPEND_CAP_INR", "55.5")
    monkeypatch.setenv(
        "LLM_PRICE_TABLE", '{"m-1": {"in_per_mtok_inr": "3", "out_per_mtok_inr": "9"}}'
    )
    s = LLMSettings()
    assert s.base_url == "https://llm.example.com/v1" and s.model == "m-1"
    assert s.timeout_seconds == 12 and s.daily_spend_cap_inr == Decimal("55.5")
    assert s.price_table["m-1"].out_per_mtok_inr == 9
    assert s.api_key.get_secret_value() == "sk-env-value-0123456789"


def test_key_is_hidden_in_repr_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    s = LLMSettings(api_key="sk-visible-never-0123456789")  # type: ignore[arg-type]
    assert "sk-visible" not in repr(s) and "sk-visible" not in str(s)
    with pytest.raises(ValidationError) as ei:
        LLMSettings(api_key="sk-visible-never-0123456789", timeout_seconds=-1)  # type: ignore[arg-type]
    assert "sk-visible" not in str(ei.value)


def test_defaults_are_bounded() -> None:
    s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.max_retries <= 3 and s.daily_spend_cap_inr > 0 and "*" in s.price_table
