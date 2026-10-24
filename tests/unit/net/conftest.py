# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Fixtures for `src.net` tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No ambient NET_*/LLM_* env vars or .env file may leak into a test."""
    for key in list(os.environ):
        if key.upper().startswith("NET_") or key.upper() == "LLM_BASE_URL":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    yield
