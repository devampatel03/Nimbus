# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Import-lint: Redis was removed in the pivot (TARGET_DESIGN M5, P1.3a).
No module under src/ or tests/ may import `redis`."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _redis_imports(root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "redis" or n.startswith("redis.") for n in names):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return hits


def test_no_redis_import_in_src_or_tests() -> None:
    hits = _redis_imports(ROOT / "src") + _redis_imports(ROOT / "tests")
    assert hits == [], f"redis imports found: {hits}"
