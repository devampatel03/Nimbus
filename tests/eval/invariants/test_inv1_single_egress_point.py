# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-1 (pivot, P1.4c, H14-approved rewrite): `src/net` is the only transport.

No module under `src/` or `config/` outside `src/net/` may import a network
transport library. All outbound traffic therefore has to go through
`src.net` (hostname allowlist, Gmail-send denial, size caps -- ADR-0004,
R13), and LLM traffic additionally through `src.llm` (scrub/audit/kill
switch/spend cap -- R4/R5; its own import-lint test asserts no module outside
`src/llm` imports an LLM SDK).

This is an import scan, not a call-site count: the previous version counted
`httpx.<x>(...)` calls and allowed exactly one (the deleted
`execute_external`); it ignored `socket`/`urllib3`/`httpx2` entirely and
needed a path-prefix hack to tolerate `src/net`.
Here *any* import of a forbidden module -- `import x`, `import x.y as z`,
`from x import y`, `from x.y import z`, function-local imports, and
string-literal `importlib.import_module("x")` / `__import__("x")` -- is a
violation unless the file is on the allow-list below.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "config")

# Top-level module names that can open a network connection.
TRANSPORT_MODULES = frozenset(
    {
        "httpx",
        "httpx2",
        "requests",
        "aiohttp",
        "urllib3",
        "httplib2",
        "socket",
        # stdlib / common extras with the same capability (hardening beyond H14's list)
        "http.client",
        "urllib.request",
        "smtplib",
        "ftplib",
        "websockets",
        "pycurl",
    }
)

# path prefix (repo-relative, posix) -> transport modules it may import.
# Keep this minimal; every entry needs a one-line justification.
ALLOWLIST: dict[str, frozenset[str]] = {
    # src/net is the sanctioned egress client (ADR-0004, R13): it builds the
    # allowlisted httpx client that src/llm hands to the OpenAI SDK.
    "src/net/": frozenset({"httpx"}),
}


def _matches(name: str) -> str | None:
    """Return the forbidden module `name` refers to, if any."""
    for mod in TRANSPORT_MODULES:
        if name == mod or name.startswith(mod + "."):
            return mod
    return None


def _imported_transports(tree: ast.AST) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            # `from http import client` -> also check "http.client"
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            )
        ):
            names = [node.args[0].value]
        for name in names:
            mod = _matches(name)
            if mod is not None:
                hits.append((node.lineno, mod))
                break
    return hits


def _allowed(rel: str, mod: str) -> bool:
    return any(rel.startswith(prefix) and mod in mods for prefix, mods in ALLOWLIST.items())


def find_violations(roots: tuple[Path, ...] = SCAN_ROOTS) -> list[str]:
    out: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno, mod in _imported_transports(tree):
                if not _allowed(rel, mod):
                    out.append(f"{rel}:{lineno} imports {mod}")
    return out


def test_no_transport_import_outside_src_net() -> None:
    violations = find_violations()
    assert violations == [], (
        "INV-1: only src/net/ may import a network transport; route egress "
        f"through src.net (and LLM calls through src.llm): {violations}"
    )


def test_allowlist_is_not_stale() -> None:
    """Each allow-list entry must still be used, so it cannot silently widen."""
    used: set[tuple[str, str]] = set()
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for _, mod in _imported_transports(tree):
                for prefix, mods in ALLOWLIST.items():
                    if rel.startswith(prefix) and mod in mods:
                        used.add((prefix, mod))
    declared = {(p, m) for p, mods in ALLOWLIST.items() for m in mods}
    assert declared == used, f"unused allow-list entries: {declared - used}"


def test_legacy_scrubber_egress_point_is_gone() -> None:
    assert not (REPO_ROOT / "src" / "security").exists(), "src/security must be deleted (M9)"


@pytest.mark.parametrize(
    "source",
    [
        "import httpx",
        "import httpx2 as h",
        "import requests.adapters",
        "from aiohttp import ClientSession",
        "from urllib3.util import Retry",
        "import httplib2",
        "def f():\n    import socket\n",
        "from http import client",
        "import urllib.request",
        "import importlib\nimportlib.import_module('socket')",
        "__import__('httpx')",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx\n",
    ],
)
def test_scanner_detects_every_import_form(source: str) -> None:
    """Self-check: the scanner flags each import shape (else INV-1 is vacuous)."""
    assert _imported_transports(ast.parse(source)), f"scanner missed: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "import json",
        "import httpx_mock_lookalike_name_only",  # prefix but not a submodule
        "from src.net import EgressClient",
        "from urllib.parse import quote",
        "import http",
    ],
)
def test_scanner_ignores_non_transport_imports(source: str) -> None:
    assert _imported_transports(ast.parse(source)) == []


def test_allowlist_does_not_cover_other_transports_inside_src_net() -> None:
    assert not _allowed("src/net/http.py", "socket")
    assert not _allowed("src/net/http.py", "requests")
    assert _allowed("src/net/http.py", "httpx")
    assert not _allowed("src/llm/adapter.py", "httpx")
