# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Scope-lint (R6, ADR-0007 proposed default, H17): the requested scope set is
exact; any scope outside it is rejected; a send method does not exist; and no
forbidden scope string appears anywhere in code or config."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.integrations.google import auth
from src.integrations.google.auth import (
    ALLOWED_SCOPES,
    BASE_SCOPES,
    DEFAULT_SCOPES,
    GoogleOAuthSettings,
    GoogleReauthRequired,
    allowed_scopes,
    lint_scopes,
    parse_scope_string,
)
from tests.unit.integrations.google.auth_helpers import (
    SCOPE_CAL,
    SCOPE_DRIVE,
    SCOPE_FULL_CAL,
    SCOPE_FULL_DRIVE,
    SCOPE_FULL_MAIL,
    SCOPE_GCOMPOSE,
    SCOPE_GREAD,
    SCOPE_MODIFY,
    SCOPE_SEND,
)

REPO = Path(__file__).resolve().parents[4]


def test_base_scope_set_is_exact_and_excludes_gmail_compose() -> None:
    """H16 is undecided: the default allowed set carries NO draft scope."""
    assert BASE_SCOPES == frozenset({SCOPE_CAL, SCOPE_GREAD, SCOPE_DRIVE})
    assert isinstance(BASE_SCOPES, frozenset)
    assert SCOPE_GCOMPOSE not in BASE_SCOPES
    assert set(DEFAULT_SCOPES) == set(BASE_SCOPES)
    assert allowed_scopes(allow_compose=False) == BASE_SCOPES
    assert len(DEFAULT_SCOPES) == len(set(DEFAULT_SCOPES))


def test_effective_set_with_compose_flag_is_base_plus_exactly_compose() -> None:
    assert allowed_scopes(allow_compose=True) == frozenset(
        {SCOPE_CAL, SCOPE_GREAD, SCOPE_GCOMPOSE, SCOPE_DRIVE}
    )
    assert allowed_scopes(allow_compose=True) == ALLOWED_SCOPES  # the absolute ceiling


def test_settings_flag_defaults_off_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert GoogleOAuthSettings(_env_file=None).allow_gmail_compose is False
    monkeypatch.setenv("GOOGLE_ALLOW_GMAIL_COMPOSE", "true")
    assert GoogleOAuthSettings(_env_file=None).allow_gmail_compose is True


def test_gmail_compose_rejected_unless_flag_on() -> None:
    with pytest.raises(GoogleReauthRequired) as exc:
        lint_scopes([SCOPE_CAL, SCOPE_GCOMPOSE])
    assert exc.value.code == "scope_violation"
    with pytest.raises(GoogleReauthRequired):
        lint_scopes([SCOPE_GCOMPOSE], allow_compose=False)
    assert lint_scopes([SCOPE_CAL, SCOPE_GCOMPOSE], allow_compose=True) == frozenset(
        {SCOPE_CAL, SCOPE_GCOMPOSE}
    )


@pytest.mark.parametrize(
    "bad",
    [
        SCOPE_SEND,
        SCOPE_MODIFY,
        SCOPE_FULL_MAIL,
        SCOPE_FULL_DRIVE,
        SCOPE_FULL_CAL,
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/gmail.metadata",
        "openid",
        "email",
        "https://www.googleapis.com/auth/userinfo.email",
        SCOPE_CAL.upper(),
        SCOPE_CAL + "/",
        " " + SCOPE_CAL,
        "",
    ],
)
@pytest.mark.parametrize("compose", [False, True])
def test_any_scope_outside_the_set_is_rejected(bad: str, compose: bool) -> None:
    with pytest.raises(GoogleReauthRequired) as exc:
        lint_scopes([SCOPE_CAL, bad], allow_compose=compose)
    assert exc.value.code == "scope_violation"
    assert bad.strip() == "" or bad not in str(exc.value)  # fixed-code message only


def test_lint_accepts_any_nonempty_subset_granular_consent() -> None:
    assert lint_scopes([SCOPE_CAL]) == frozenset({SCOPE_CAL})
    assert lint_scopes(DEFAULT_SCOPES) == BASE_SCOPES


def test_lint_rejects_empty_grant() -> None:
    with pytest.raises(GoogleReauthRequired) as exc:
        lint_scopes([])
    assert exc.value.code == "scope_violation"


def test_parse_scope_string_splits_on_whitespace_and_dedupes() -> None:
    assert parse_scope_string(f"{SCOPE_CAL}  {SCOPE_GREAD}\n{SCOPE_CAL}") == frozenset(
        {SCOPE_CAL, SCOPE_GREAD}
    )
    assert parse_scope_string("") == frozenset()


def test_lint_consults_the_constant_so_widening_it_would_admit_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the guard can fail: it rejects `send` now, and only a tampered
    constant would admit it."""
    with pytest.raises(GoogleReauthRequired):
        lint_scopes([SCOPE_SEND])
    monkeypatch.setattr(auth, "BASE_SCOPES", BASE_SCOPES | {SCOPE_SEND})
    assert lint_scopes([SCOPE_SEND]) == frozenset({SCOPE_SEND})


# ---- repo-wide lint: forbidden scope strings anywhere in code or config ----

_SCAN_DIRS = ("src", "scripts", "config", "infra", "personality", "migrations", "quality")
_SCAN_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".sh", ".ps1", ".sql"}
_SCAN_ROOT_FILES = ("pyproject.toml", "docker-compose.yml", "alembic.ini")
_FORBIDDEN_RE = re.compile(
    r"gmail\.send|gmail\.modify|mail\.google\.com|gmail\.insert|gmail\.settings",
    re.IGNORECASE,
)
_SCOPE_URL_RE = re.compile(r"googleapis\.com/auth/[A-Za-z0-9_.\-/]*")


def _scanned_files() -> list[Path]:
    files: list[Path] = [REPO / name for name in _SCAN_ROOT_FILES if (REPO / name).exists()]
    for d in _SCAN_DIRS:
        root = REPO / d
        if root.is_dir():
            files += [
                p
                for p in root.rglob("*")
                if p.is_file()
                and p.suffix in _SCAN_SUFFIXES
                and "__pycache__" not in p.parts
                and not p.name.startswith(".env")
            ]
    return files


def _violations(text: str) -> list[str]:
    hits = [m.group(0) for m in _FORBIDDEN_RE.finditer(text)]
    for m in _SCOPE_URL_RE.finditer(text):
        url = "https://www." + m.group(0).rstrip(".,")
        if url not in ALLOWED_SCOPES:
            hits.append(m.group(0))
    return hits


def test_scan_actually_covers_the_google_package() -> None:
    names = {p.name for p in _scanned_files()}
    assert "auth.py" in names and "pyproject.toml" in names


def test_no_forbidden_or_unlisted_scope_string_in_code_or_config() -> None:
    bad = {
        str(p.relative_to(REPO)): hits
        for p in _scanned_files()
        if (hits := _violations(p.read_text(encoding="utf-8", errors="replace")))
    }
    assert bad == {}


@pytest.mark.parametrize(
    "text",
    [
        f'SCOPES = ["{SCOPE_SEND}"]',
        f"scope: {SCOPE_MODIFY}",
        f"scopes = ['{SCOPE_FULL_MAIL}']",
        f'x = "{SCOPE_FULL_DRIVE}"',
        f'x = "{SCOPE_FULL_CAL}"',
        'x = "https://www.googleapis.com/auth/drive.file"',
    ],
)
def test_scan_detector_can_fail(text: str) -> None:
    assert _violations(text)


def test_scan_detector_passes_allowed_scopes() -> None:
    assert _violations("\n".join(sorted(ALLOWED_SCOPES))) == []


def test_no_send_method_exists_in_google_package() -> None:
    """R6: there is no code path named send/import/insert in the Google package."""
    pkg = REPO / "src" / "integrations"
    offenders: list[str] = []
    for path in pkg.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.append(node.name)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            offenders += [
                f"{path.name}:{n}" for n in names if re.search(r"(^|_)send($|_)", n.lower())
            ]
    assert offenders == []


def test_module_exposes_no_send_attribute() -> None:
    assert not [n for n in dir(auth) if re.search(r"(^|_)send($|_)", n.lower())]
