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
violation unless the file is on the allow-list below. Also flagged: any load of
`import_module`/`__import__` (not just calls; non-literal arguments fail
closed), bare `exec`/`eval`/`compile`, `urllib.request.*`/`http.client.*`
attribute chains (import aliases resolved), and connect-by-name calls
(`open_connection`, `create_connection`, `sock_connect`, ...).

NOT detected (known gaps; a determined in-repo author can evade them):
`getattr(module, "name")` tricks, `Service.__members__`/`for s in Service`
enumeration reaching `Service.LLM`, `globals()["__import__"]`, imports built by
string concatenation inside `exec`d code (exec itself is flagged), and
third-party libraries that open sockets internally without a flagged import.
This is a lint for accidents and review-visible shortcuts, not a sandbox; the
runtime zero-transport check (guard tests) and the network allowlist back it.
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
    # pkgutil-based tool discovery imports sibling modules of src/tools/integrations
    # by computed name; it cannot name a transport (the package is fixed).
    "src/tools/registry.py": frozenset({"dynamic-import"}),
}


LLM_SDKS = frozenset(
    {
        "openai",
        "anthropic",
        "google.generativeai",
        "google.genai",
        "litellm",
        "mistralai",
        "cohere",
        "groq",
        "together",
        "huggingface_hub.inference",
    }
)

# Connection-opening callables flagged *by name* (receiver unknowable statically:
# `loop.create_connection`). Labelled `call:<name>` in hits.
CONNECT_CALLS = frozenset(
    {
        "open_connection",
        "create_connection",
        "open_unix_connection",
        "create_unix_connection",
        "create_datagram_endpoint",
        "sock_connect",
    }
)
_DYNAMIC_IMPORTERS = frozenset({"__import__", "import_module"})
# Dynamic code execution: can build any import/call from a string.
_DYNAMIC_CODE = frozenset({"exec", "eval", "compile"})
# Attribute-chain prefixes that reach a transport without a matching import
# statement (`import urllib` + `urllib.request.urlopen(...)` after another
# module imported the submodule).
CHAIN_PREFIXES = ("urllib.request", "http.client")


def _matches(name: str, modules: frozenset[str] = TRANSPORT_MODULES) -> str | None:
    """Return the forbidden module `name` refers to, if any."""
    for mod in modules:
        if name == mod or name.startswith(mod + "."):
            return mod
    return None


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _scan(tree: ast.AST, modules: frozenset[str], *, with_calls: bool) -> list[tuple[int, str]]:
    """Hits for `modules`: static imports, dynamic imports (any spelling of
    `__import__`/`import_module`, bare or via importlib/builtins), and -- when
    `with_calls` -- attribute chains and connect-by-name calls. A dynamic import
    whose module name is not a string literal is itself a hit (`dynamic-import`):
    it cannot be proven safe, so it fails closed."""
    hits: list[tuple[int, str]] = []
    importers = set(_DYNAMIC_IMPORTERS)
    aliases: dict[str, str] = {}  # local name -> full dotted module/object
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            importers |= {a.asname for a in node.names if a.asname and a.name in _DYNAMIC_IMPORTERS}
            if node.module and node.level == 0:
                for a in node.names:
                    aliases[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
    for node in ast.walk(tree):
        names: list[str] = []
        label: str | None = None
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            if with_calls and node.module == "asyncio":
                for a in node.names:
                    if a.name in CONNECT_CALLS:
                        label = f"call:{a.name}"
        elif isinstance(node, ast.Call):
            callee = _callee(node.func)
            if callee in importers:
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names = [arg.value]
                elif with_calls:
                    label = "dynamic-import"
            elif with_calls and callee in CONNECT_CALLS:
                label = f"call:{callee}"
            elif (
                with_calls
                and callee in _DYNAMIC_CODE
                # bare exec/eval/compile or builtins.X -- NOT re.compile etc.
                and (isinstance(node.func, ast.Name) or _dotted(node.func) == f"builtins.{callee}")
            ):
                label = "dynamic-code"
        elif with_calls and isinstance(node, ast.Name | ast.Attribute):
            # Any *load* of import_module/__import__ (alias assignment, passing
            # it around), not only a direct call.
            ident = node.id if isinstance(node, ast.Name) else node.attr
            if ident in importers and isinstance(node.ctx, ast.Load):
                label = "dynamic-import"
            dotted = _dotted(node) if isinstance(node, ast.Attribute) else None
            if dotted is not None:
                root, _, rest = dotted.partition(".")
                if root in aliases:
                    dotted = aliases[root] + ("." + rest if rest else "")
                for prefix in CHAIN_PREFIXES:
                    if label is None and (dotted == prefix or dotted.startswith(prefix + ".")):
                        label = prefix
        if label is None:
            for name in names:
                label = _matches(name, modules)
                if label is not None:
                    break
        if label is not None:
            hits.append((node.lineno, label))
    return hits


def _imported_transports(tree: ast.AST) -> list[tuple[int, str]]:
    return _scan(tree, TRANSPORT_MODULES, with_calls=True)


def _imported_llm_sdks(tree: ast.AST) -> list[tuple[int, str]]:
    return _scan(tree, LLM_SDKS, with_calls=False)


def _service_llm_refs(tree: ast.AST) -> list[int]:
    """Lines referencing `Service.LLM` (TARGET_DESIGN section 3 part 3).

    Catches `Service.LLM`, `x.Service.LLM`, aliases (`from ... import Service as S`),
    `Service["LLM"]`, `Service("llm")` and `getattr(Service, "LLM")`."""
    aliases = {"Service"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "Service" and a.asname:
                    aliases.add(a.asname)

    def is_service(expr: ast.expr) -> bool:
        d = _dotted(expr)
        return d is not None and d.split(".")[-1] in aliases

    def is_llm_const(expr: ast.expr) -> bool:
        return (
            isinstance(expr, ast.Constant)
            and isinstance(expr.value, str)
            and expr.value.lower() == "llm"
        )

    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "LLM"
            and is_service(node.value)
            or (
                isinstance(node, ast.Subscript)
                and is_service(node.value)
                and is_llm_const(node.slice)
            )
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.Call) and node.args:
            direct = is_service(node.func) and is_llm_const(node.args[0])
            via_getattr = (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and is_service(node.args[0])
                and is_llm_const(node.args[1])
            )
            if direct or via_getattr:
                lines.append(node.lineno)
    return lines


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


def find_llm_sdk_violations(roots: tuple[Path, ...] = SCAN_ROOTS) -> list[str]:
    out: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith("src/llm/"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            out += [f"{rel}:{n} imports {m}" for n, m in _imported_llm_sdks(tree)]
    return out


# Service.LLM may be referenced only under src/llm/ -- plus src/net/policy.py,
# which defines it and must map it to the configured host (TARGET_DESIGN 3 part 3).
SERVICE_LLM_ALLOWED = ("src/llm/", "src/net/policy.py")


def find_service_llm_violations(roots: tuple[Path, ...] = SCAN_ROOTS) -> list[str]:
    out: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(SERVICE_LLM_ALLOWED):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            out += [f"{rel}:{n} references Service.LLM" for n in _service_llm_refs(tree)]
    return out


def test_no_llm_sdk_import_outside_src_llm() -> None:
    """TARGET_DESIGN section 3 part 1, over src/ AND config/."""
    assert find_llm_sdk_violations() == []


def test_service_llm_only_referenced_under_src_llm() -> None:
    """TARGET_DESIGN section 3 part 3."""
    assert find_service_llm_violations() == []


def test_service_llm_is_defined_in_net_policy() -> None:
    tree = ast.parse((REPO_ROOT / "src/net/policy.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Service")
    assigns = [
        t.id
        for n in cls.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    ]
    assert "LLM" in assigns


# Parts 4 and 5 of the section-3 INV-1 spec are not static scans and live elsewhere:
#   part 4 (kill switch / spend cap / scrub hit => zero transport calls)
#       -> tests/unit/llm/test_llm_guard_r4.py (RecordingTransport.calls == [])
#   part 5 (HF_HUB_OFFLINE=1 asserted at runtime for the local embedder)
#       -> OWNER: P2.3 (embedder wiring); not testable before that code exists.


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.__import__('socket')",
        "import builtins\nbuiltins.__import__('httpx')",
        "from importlib import import_module\nimport_module('requests')",
        "from importlib import import_module as im\nim('aiohttp')",
        "import importlib\nimportlib.import_module(name)",  # non-literal: fail closed
        "__import__(name)",
        "import urllib\nurllib.request.urlopen(u)",
        "import http\nc = http.client.HTTPSConnection(h)",
        "import asyncio\nasync def f():\n    await asyncio.open_connection(h, 443)",
        "async def f(loop):\n    await loop.create_connection(p, h, 443)",
        "from asyncio import open_connection",
    ],
)
def test_scanner_detects_hardened_forms(source: str) -> None:
    assert _imported_transports(ast.parse(source)), f"scanner missed: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "import openai",
        "from openai import AsyncOpenAI",
        "from google import genai",
        "import anthropic.types",
        "__import__('litellm')",
        "import importlib\nimportlib.import_module('cohere')",
        "from huggingface_hub import inference",
    ],
)
def test_llm_sdk_scanner_detects(source: str) -> None:
    assert _imported_llm_sdks(ast.parse(source)), f"scanner missed: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "import json",
        "import google.auth",
        "from src.llm import GuardedLLM",
        "import huggingface_hub",
    ],
)
def test_llm_sdk_scanner_ignores_benign(source: str) -> None:
    assert _imported_llm_sdks(ast.parse(source)) == []


@pytest.mark.parametrize(
    "source",
    [
        "from src.net import Service\nx = Service.LLM",
        "from src.net.policy import Service as S\nx = S.LLM",
        "import src.net.policy as p\nx = p.Service.LLM",
        "x = Service['LLM']",
        "x = Service('llm')",
        "x = getattr(Service, 'LLM')",
    ],
)
def test_service_llm_scanner_detects(source: str) -> None:
    assert _service_llm_refs(ast.parse(source)), f"scanner missed: {source!r}"


@pytest.mark.parametrize(
    "source",
    ["x = Service.TELEGRAM", "x = Other.LLM", "LLM = 1", "x = Service('telegram')"],
)
def test_service_llm_scanner_ignores_benign(source: str) -> None:
    assert _service_llm_refs(ast.parse(source)) == []


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nf = importlib.import_module",  # reference, not call
        "from importlib import import_module\nf = import_module",
        "f = __import__",
        "import builtins\nf = builtins.__import__",
        "exec('import socket')",
        "eval('1')",
        "compile('x', 'f', 'exec')",
        "import urllib as u\nu.request.urlopen(x)",
        "import http as h\nh.client.HTTPSConnection(x)",
        "from urllib import request as r\nr.urlopen(x)",
        "from http import client as c\nc.HTTPSConnection(x)",
        "async def f(loop):\n    await loop.sock_connect(s, a)",
    ],
)
def test_scanner_detects_round1_forms(source: str) -> None:
    assert _imported_transports(ast.parse(source)), f"scanner missed: {source!r}"


def test_re_compile_is_not_dynamic_code() -> None:
    assert _imported_transports(ast.parse("import re\nre.compile('x')")) == []


def test_dynamic_code_is_not_flagged_by_llm_sdk_scan() -> None:
    assert _imported_llm_sdks(ast.parse("exec('x')")) == []


def test_asyncio_connect_by_name_is_forbidden_even_in_src_net() -> None:
    assert not _allowed("src/net/http.py", "call:open_connection")


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
