# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""scripts/google_oauth.py: scope names map onto the exact allowed set, the
live smoke test is env-gated, and the script imports no network transport
(INV-1 holds for scripts too, not only for src/)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from src.db.crypto import KEY_ENV_VAR, generate_key
from src.integrations.google.auth import ALLOWED_SCOPES

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "google_oauth.py"
TRANSPORTS = {"httpx", "requests", "aiohttp", "urllib3", "socket", "http", "smtplib", "urllib"}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("google_oauth_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_scope_names_cover_exactly_the_allowed_set() -> None:
    mod = _load()
    assert set(mod.SCOPE_NAMES.values()) == set(ALLOWED_SCOPES)
    assert set(mod._resolve_scopes(",".join(mod.SCOPE_NAMES))) == set(ALLOWED_SCOPES)


@pytest.mark.parametrize("raw", ["send", "gmail", "drive", "", " , "])
def test_script_rejects_unknown_or_empty_scope_names(raw: str) -> None:
    with pytest.raises(SystemExit):
        _load()._resolve_scopes(raw)


def test_script_imports_no_transport() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(TRANSPORTS)


def test_smoke_is_skipped_without_live_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(KEY_ENV_VAR, generate_key())
    monkeypatch.delenv("NIMBUS_LIVE_GOOGLE", raising=False)
    assert _load().main(["smoke"]) == 3
    assert "SKIPPED" in capsys.readouterr().out


def test_script_fails_cleanly_without_data_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(KEY_ENV_VAR, raising=False)
    assert _load().main(["status"]) == 2
    assert "NIMBUS_DATA_KEY" in capsys.readouterr().out
