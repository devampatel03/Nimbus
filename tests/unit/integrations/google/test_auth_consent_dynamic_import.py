# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""R3 follow-up (P3.4): the consent-isolation scan in
`test_auth_consent_isolation.py` sees only `import` statements. This closes the
dynamic route: no module under `src/` (other than consent.py itself) may name
the consent module in a string, nor pass a non-literal to `import_module` /
`__import__` outside the reviewed tool registry. Fails closed: an unknown
dynamic import is an offender."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SRC = REPO / "src"
CONSENT = SRC / "integrations" / "google" / "consent.py"
# The only reviewed dynamic import in src (auto-discovery of src/tools/integrations).
DYNAMIC_IMPORT_SITES = frozenset({"src/tools/registry.py"})
_IMPORTERS = frozenset({"import_module", "__import__", "find_spec", "spec_from_file_location"})


def _names_consent(value: str) -> bool:
    v = value.replace("/", ".").replace("\\", ".")
    return "google.consent" in v or v in ("consent", ".consent")


def dynamic_consent_problems(source: str, rel: str) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _names_consent(node.value)
        ):
            problems.append(f"names consent at line {node.lineno}")
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("importlib")
            and any(a.name in _IMPORTERS for a in node.names)
            and rel not in DYNAMIC_IMPORT_SITES
        ):
            problems.append(f"imports a dynamic importer at line {node.lineno}")
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in _IMPORTERS and rel not in DYNAMIC_IMPORT_SITES:
                problems.append(f"dynamic import {name} at line {node.lineno}")
    return problems


def _src_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts and p != CONSENT]


def test_no_src_module_reaches_consent_dynamically() -> None:
    offenders = {
        p.relative_to(REPO).as_posix(): hits
        for p in _src_files()
        if (hits := dynamic_consent_problems(p.read_text("utf-8"), p.relative_to(REPO).as_posix()))
    }
    assert offenders == {}


def test_the_reviewed_dynamic_import_site_cannot_reach_consent() -> None:
    """The registry imports only submodules of src/tools/integrations."""
    reg = (SRC / "tools" / "registry.py").read_text("utf-8")
    assert "from src.tools import integrations" in reg
    assert not (SRC / "tools" / "integrations" / "consent.py").exists()


@pytest.mark.parametrize(
    "snippet",
    [
        "import importlib\nimportlib.import_module('src.integrations.google.consent')",
        "importlib.import_module('.consent', 'src.integrations.google')",
        "__import__('src.integrations.google.consent')",
        "m = 'src/integrations/google/consent'",
        "from importlib import import_module as im\nim(name)",
        "import importlib\nimportlib.import_module(name)",
        "importlib.util.spec_from_file_location('x', path)",
    ],
)
def test_detector_can_fail(snippet: str) -> None:
    assert dynamic_consent_problems(snippet, "src/app/x.py")


def test_detector_passes_innocent_code() -> None:
    assert dynamic_consent_problems("x = 'consent screen'\nimport json", "src/app/x.py") == []
    assert dynamic_consent_problems("importlib.import_module(n)", "src/tools/registry.py") == []
