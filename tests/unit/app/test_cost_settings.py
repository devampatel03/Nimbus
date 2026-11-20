# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P5.5 cost levers in `AppSettings`: env names, defaults equal today's
behaviour, and bounded ranges."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.app.settings import AppSettings
from src.orchestrator.context import MAX_MEMORY_CHARS
from src.scheduler.memory_extract import MAX_SPEND_SHARE, MIN_CHARS


def test_defaults_keep_current_behaviour() -> None:
    s = AppSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.memory_top_k == 5 and s.memory_item_chars == MAX_MEMORY_CHARS
    assert s.extract_min_chars == MIN_CHARS and s.extract_mode == "all"
    assert s.extract_max_spend_share == MAX_SPEND_SHARE


def test_cheap_profile_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_MEMORY_TOP_K", "3")
    monkeypatch.setenv("APP_MEMORY_ITEM_CHARS", "400")
    monkeypatch.setenv("APP_EXTRACT_MODE", "cue")
    monkeypatch.setenv("APP_EXTRACT_MIN_CHARS", "60")
    s = AppSettings()
    assert (s.memory_top_k, s.memory_item_chars, s.extract_mode, s.extract_min_chars) == (
        3,
        400,
        "cue",
        60,
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"memory_top_k": 21},
        {"memory_item_chars": 50},
        {"extract_mode": "always"},
        {"extract_min_chars": 0},
        {"extract_max_spend_share": 0},
    ],
)
def test_bounds(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, **kw)  # type: ignore[arg-type]
