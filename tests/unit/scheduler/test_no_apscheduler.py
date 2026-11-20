# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""APScheduler is gone (ADR-0008, P3.3): nothing under `src/` imports it, and the
scheduler package stays free of transports and model SDKs (INV-1)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src"
FORBIDDEN_IN_SCHEDULER = {"httpx", "openai", "requests", "aiohttp", "urllib3", "socket"}


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_no_module_imports_apscheduler() -> None:
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "apscheduler" in _imports(p)]
    assert offenders == []


def test_scheduler_package_imports_no_transport_or_sdk() -> None:
    files = list((SRC / "scheduler").glob("*.py"))
    assert files, "src/scheduler must exist"
    bad = {str(p.name): _imports(p) & FORBIDDEN_IN_SCHEDULER for p in files}
    assert {k: v for k, v in bad.items() if v} == {}


def test_scheduler_has_no_dynamic_imports_or_eval() -> None:
    for p in (SRC / "scheduler").glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "importlib" not in text and "__import__" not in text and "eval(" not in text
