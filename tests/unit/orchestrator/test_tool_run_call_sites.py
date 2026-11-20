# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Structural check (INV-5): a `LoopTool` is executed only by the `Dispatcher`,
after the Policy has decided. Any other `.run(` call site in `src/` could
bypass the policy, so the set of files calling `.run(` is pinned."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src"
ALLOWED = {"src/orchestrator/dispatch.py"}


def _is_tool_run(n: ast.AST) -> bool:
    if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run"):
        return False
    # asyncio.run(...) is the event-loop entry point, not a tool call.
    recv = n.func.value
    return not (isinstance(recv, ast.Name) and recv.id == "asyncio")


def run_call_sites(root: Path) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*.py"):
        if any(_is_tool_run(n) for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))):
            found.add(path.relative_to(root.parent).as_posix())
    return found


def test_tools_are_run_only_by_the_dispatcher() -> None:
    assert run_call_sites(SRC) <= ALLOWED


def test_dispatcher_is_actually_a_call_site() -> None:
    assert "src/orchestrator/dispatch.py" in run_call_sites(SRC)


@pytest.mark.parametrize(
    "snippet",
    [
        "async def f(tool):\n    return await tool.run({})\n",
        "async def f(self):\n    await self._tools['x'].run({})\n",
    ],
)
def test_scanner_detects_a_rogue_run_call(tmp_path: Path, snippet: str) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "rogue.py").write_text(snippet, encoding="utf-8")
    assert run_call_sites(root) == {"src/rogue.py"}


def test_asyncio_run_is_not_flagged(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    source = "import asyncio\n\nasyncio.run(main())\n"
    (root / "ok.py").write_text(source, encoding="utf-8")
    assert run_call_sites(root) == set()
