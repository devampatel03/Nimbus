# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""R3: the consent flow (the only code that opens a listening socket) is not
reachable from the bot. `src/integrations/google/consent.py` is imported only
by `scripts/`, and no other module under `src/` starts a server. Also pins the
constant-time `state` comparison."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SRC = REPO / "src"
CONSENT = SRC / "integrations" / "google" / "consent.py"
CONSENT_MODULE = "src.integrations.google.consent"
SERVER_CALLS = frozenset({"start_server", "create_server", "start_unix_server", "serve_forever"})
SERVER_MODULES = ("http.server", "socketserver", "wsgiref", "aiohttp.web", "uvicorn", "hypercorn")
BOT_PACKAGES = ("app", "gateway", "orchestrator", "tools", "ingest", "workers", "scheduler")


def imports_consent(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(
                a.name == CONSENT_MODULE or a.name.startswith(CONSENT_MODULE + ".")
                for a in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == CONSENT_MODULE or mod.startswith(CONSENT_MODULE + "."):
                return True
            if mod == "src.integrations.google" and any(a.name == "consent" for a in node.names):
                return True
            if node.level and mod.endswith("consent"):
                return True
            if node.level and any(a.name == "consent" for a in node.names):
                return True
    return False


def starts_server(source: str) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in SERVER_CALLS:
                hits.append(name)
        elif isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.startswith(SERVER_MODULES)]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(SERVER_MODULES):
            hits.append(node.module or "")
    return hits


def _src_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_consent_module_exists_and_is_where_the_flow_lives() -> None:
    assert CONSENT.is_file()
    text = CONSENT.read_text(encoding="utf-8")
    assert "class LoopbackServer" in text and "def authorize_interactive" in text


def test_no_src_module_imports_consent() -> None:
    offenders = [
        str(p.relative_to(REPO)) for p in _src_files() if imports_consent(p.read_text("utf-8"))
    ]
    assert offenders == []


def test_bot_packages_exist_so_the_isolation_scan_is_meaningful() -> None:
    present = [d for d in BOT_PACKAGES if (SRC / d).is_dir()]
    assert {"app", "gateway", "orchestrator", "tools", "ingest"} <= set(present)


def test_only_consent_starts_a_server_in_src() -> None:
    offenders = {
        str(p.relative_to(REPO)): hits
        for p in _src_files()
        if p != CONSENT and (hits := starts_server(p.read_text("utf-8")))
    }
    assert offenders == {}
    assert starts_server(CONSENT.read_text("utf-8")) == ["start_server"]


def test_script_is_the_importer_of_consent() -> None:
    script = (REPO / "scripts" / "google_oauth.py").read_text(encoding="utf-8")
    assert imports_consent(script)


@pytest.mark.parametrize(
    "snippet",
    [
        "from src.integrations.google.consent import authorize_interactive",
        "import src.integrations.google.consent as c",
        "from src.integrations.google import consent",
        "from .consent import LoopbackServer",
        "from . import consent",
    ],
)
def test_import_detector_can_fail(snippet: str) -> None:
    assert imports_consent(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "import asyncio\nawait asyncio.start_server(h, '127.0.0.1', 0)",
        "loop.create_server(f)",
        "import http.server",
        "from socketserver import TCPServer",
    ],
)
def test_server_detector_can_fail(snippet: str) -> None:
    assert starts_server(snippet)


def test_detectors_pass_innocent_code() -> None:
    assert not imports_consent("from src.integrations.google.auth import GoogleAuth")
    assert not starts_server("import asyncio\nasyncio.sleep(1)")


# ---- state comparison is constant time (pinned by AST) ----------------------

_STATE_NAMES = frozenset({"state", "expected_state", "_expected_state"})


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def state_comparison_problems(source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    calls = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and _name(n.func) == "compare_digest"
    ]
    if not calls:
        problems.append("no compare_digest call")
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(o, ast.Eq | ast.NotEq) for o in node.ops
        ):
            operands = [node.left, *node.comparators]
            for op in operands:
                inner = op.value if isinstance(op, ast.Subscript) else op
                if _name(inner) in _STATE_NAMES or (
                    isinstance(inner, ast.Call)
                    and any(
                        _name(a) in _STATE_NAMES
                        for a in ast.walk(inner)
                        if isinstance(a, ast.Name | ast.Attribute)
                    )
                ):
                    problems.append(f"==/!= on state at line {node.lineno}")
    return problems


def test_state_is_compared_with_compare_digest_only() -> None:
    assert state_comparison_problems(CONSENT.read_text("utf-8")) == []


def test_state_comparison_detector_can_fail() -> None:
    assert state_comparison_problems("def f(params, state):\n    return params.state != state\n")
    assert state_comparison_problems("def f(a, state):\n    return a == state\n")
    assert state_comparison_problems("x = 1")  # no compare_digest at all
    ok = "import hmac\ndef f(a, state):\n    return hmac.compare_digest(a, state)\n"
    assert state_comparison_problems(ok) == []
