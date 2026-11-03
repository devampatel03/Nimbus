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


# ---------------------------------------------- free-tier pricing warning (ADR-0025)

_KEY = "sk-free-never-logged-0123456789"  # pragma: allowlist secret


def _s(**kw: object) -> LLMSettings:
    return LLMSettings(_env_file=None, api_key=_KEY, **kw)  # type: ignore[arg-type, call-arg]


@pytest.mark.parametrize(
    ("kw", "free"),
    [
        ({}, ()),  # placeholder `*` price
        ({"price_in_per_mtok_inr": "0.0001", "price_out_per_mtok_inr": "0.0001"}, ("m",)),
        ({"price_in_per_mtok_inr": "5", "price_out_per_mtok_inr": "0.0099"}, ("m",)),
        ({"price_in_per_mtok_inr": "0.01", "price_out_per_mtok_inr": "0.01"}, ()),  # boundary
        (
            {
                "model_cheap": "c",
                "cheap_price_in_per_mtok_inr": "0.0001",
                "cheap_price_out_per_mtok_inr": "0.0001",
            },
            ("c",),
        ),
        (
            {"price_table": {"*": {"in_per_mtok_inr": "0.001", "out_per_mtok_inr": "0.001"}}},
            ("m",),  # an unlisted model falls back to a free-tier `*`
        ),
    ],
)
def test_free_tier_priced_models(kw: dict[str, object], free: tuple[str, ...]) -> None:
    assert _s(model="m", **kw).free_tier_priced_models() == free


def test_free_tier_pricing_logs_one_warning_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.llm.settings import warn_if_free_tier_pricing

    s = _s(
        model="secret-model-name",
        base_url="https://llm.example.com/v1",
        price_in_per_mtok_inr="0.0001",
        price_out_per_mtok_inr="0.0001",
        rpm_limit=10,
        daily_request_limit=900,
    )
    with caplog.at_level("WARNING", logger="src.llm.settings"):
        assert warn_if_free_tier_pricing(s) is True
    (rec,) = caplog.records
    msg = rec.getMessage()
    assert rec.levelname == "WARNING"
    assert "NOT a real bound" in msg and "request limits" in msg
    assert "rpm=10" in msg and "daily=900" in msg
    for leak in (_KEY, "secret-model-name", "llm.example.com"):
        assert leak not in msg


def test_paid_pricing_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    from src.llm.settings import warn_if_free_tier_pricing

    with caplog.at_level("DEBUG", logger="src.llm.settings"):
        assert warn_if_free_tier_pricing(_s(model="m")) is False
    assert caplog.records == []


def test_runtime_warns_at_startup() -> None:
    """The check is wired into `run` before the one GuardedLLM is built."""
    src = (Path(__file__).parents[3] / "src" / "app" / "runtime.py").read_text(encoding="utf-8")
    body = src[src.index("async def run_app(") :]
    assert body.index("warn_if_free_tier_pricing(cfg.llm)") < body.index("build_guarded_llm(")
