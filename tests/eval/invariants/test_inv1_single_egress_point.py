# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-1: Exactly one function performs cloud model egress.

Acceptance: `src/security/scrubber.py:execute_external` is the sole call
site that sends data to an external/cloud model. This statically scans
`src/` for outbound HTTP client usage (httpx/requests/aiohttp) via AST and
asserts it only appears inside that function.

Self-adjusting per T0.10: today the scrubber does not exist, so this
asserts zero egress call sites anywhere in `src/`. Once
`src/security/scrubber.py` exists, it asserts exactly one call site, and
that it lives inside `execute_external` in that file.
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src"
EGRESS_MODULES = {"httpx", "requests", "aiohttp"}
SCRUBBER_FILE = SRC_ROOT / "security" / "scrubber.py"
SCRUBBER_FUNCTION = "execute_external"
# P1.4a (H14-approved edit): src/net is the sanctioned transport layer (ADR-0004);
# its httpx use is exempt here. Any httpx/requests/aiohttp call elsewhere in src/
# must still be exactly the scrubber's. P1.4c rewrites this test around src/net/src/llm.
SANCTIONED_TRANSPORT_PREFIX = "src/net/"


def _egress_aliases(tree: ast.Module) -> set[str]:
    """Names in this module bound to an egress-capable module or symbol."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in EGRESS_MODULES:
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] in EGRESS_MODULES
        ):
            for alias in node.names:
                aliases.add(alias.asname or alias.name)
    return aliases


class _CallCollector(ast.NodeVisitor):
    """Records (lineno, enclosing_function) for calls that resolve to an
    egress-capable name, e.g. `httpx.post(...)` or `AsyncClient(...)`.
    """

    def __init__(self, aliases: set[str]) -> None:
        self._aliases = aliases
        self._func_stack: list[str] = []
        self.hits: list[tuple[int, str | None]] = []

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = node.func
        is_egress_call = (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in self._aliases
        ) or (isinstance(target, ast.Name) and target.id in self._aliases)
        if is_egress_call:
            enclosing = self._func_stack[-1] if self._func_stack else None
            self.hits.append((node.lineno, enclosing))
        self.generic_visit(node)


def _find_egress_call_sites() -> list[tuple[str, int, str | None]]:
    hits: list[tuple[str, int, str | None]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _CallCollector(_egress_aliases(tree))
        collector.visit(tree)
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        hits.extend((rel, lineno, func) for lineno, func in collector.hits)
    return hits


def test_only_scrubber_execute_external_calls_cloud_model():
    hits = [
        h for h in _find_egress_call_sites() if not h[0].startswith(SANCTIONED_TRANSPORT_PREFIX)
    ]

    if not SCRUBBER_FILE.exists():
        assert (
            hits == []
        ), f"INV-1: found outbound HTTP call site(s) before the scrubber exists: {hits}"
        return

    assert (
        len(hits) == 1
    ), f"INV-1: expected exactly one cloud egress call site, found {len(hits)}: {hits}"
    file_path, _, func_name = hits[0]
    assert (
        file_path == "src/security/scrubber.py"
    ), f"INV-1: egress call site is outside the scrubber: {hits[0]}"
    assert (
        func_name == SCRUBBER_FUNCTION
    ), f"INV-1: egress call site is not inside {SCRUBBER_FUNCTION}: {hits[0]}"
