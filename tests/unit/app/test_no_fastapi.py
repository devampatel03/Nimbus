# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""M11 / ADR-0012: the HTTP API is gone. No module may import the web stack or
the JWT library, and `src/api` must not exist."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BANNED = ("fastapi", "starlette", "uvicorn", "jwt")


def _hits(root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] in BANNED for n in names):
                out.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return out


def test_no_web_framework_or_jwt_import_anywhere() -> None:
    hits = [h for d in ("src", "tests", "config", "scripts") for h in _hits(ROOT / d)]
    assert hits == []


def test_src_api_is_deleted() -> None:
    assert not (ROOT / "src" / "api").exists()


def test_dead_auth_settings_are_gone() -> None:
    from config.settings import Settings

    fields = set(Settings.model_fields)
    assert not {n for n in fields if n.startswith(("jwt_", "auth_", "cors_"))}
