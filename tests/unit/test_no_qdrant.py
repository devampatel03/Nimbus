# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P1.3b import-lint: Qdrant is retired (ADR-0001); nothing may import it."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_module_imports_qdrant_client() -> None:
    offenders = []
    for base in ("src", "tests", "config"):
        for path in (ROOT / base).rglob("*.py"):
            if path == Path(__file__).resolve():
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                if any(n.split(".")[0] == "qdrant_client" for n in names):
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
