# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-1 / R4 import lint: no module outside `src/llm` imports an LLM SDK.

Scoped to the target rule (TARGET_DESIGN section 3 part 1). The transport
scan (httpx only under src/net) belongs to the P1.4c INV-1 rewrite.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src"
LLM_SDKS = (
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
)

# Explicit allow-list of known violators: path (relative to src/) -> removing task.
# Empty today; add an entry with a TODO naming the task rather than weakening the rule.
ALLOWED_VIOLATORS: dict[str, str] = {}


def _is_sdk(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in LLM_SDKS)


def violations(root: Path, *, skip: tuple[str, ...] = ("llm",)) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] in skip:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                mods = [node.args[0].value]
            if any(_is_sdk(m) for m in mods):
                found.append(rel.as_posix())
                break
    return found


def test_no_llm_sdk_import_outside_src_llm() -> None:
    bad = [v for v in violations(SRC) if v not in ALLOWED_VIOLATORS]
    assert bad == [], f"LLM SDK imported outside src/llm: {bad}"


def test_allow_list_has_no_stale_entries() -> None:
    current = set(violations(SRC))
    assert set(ALLOWED_VIOLATORS) <= current


def test_lint_actually_detects_violations(tmp_path: Path) -> None:
    pkg = tmp_path / "other"
    pkg.mkdir()
    (pkg / "a.py").write_text("import openai\n")
    (pkg / "b.py").write_text("from google import genai\n")
    (pkg / "c.py").write_text("from anthropic.types import X\n")
    (pkg / "d.py").write_text("x = __import__('litellm')\n")
    (pkg / "ok.py").write_text("import httpx\nimport google.auth\n")
    (tmp_path / "llm").mkdir()
    (tmp_path / "llm" / "adapter.py").write_text("import openai\n")

    assert violations(tmp_path) == ["other/a.py", "other/b.py", "other/c.py", "other/d.py"]


def test_openai_is_imported_only_by_the_adapter() -> None:
    importers = [
        p.relative_to(SRC).as_posix()
        for p in (SRC / "llm").rglob("*.py")
        if "import openai" in p.read_text(encoding="utf-8")
        or "from openai" in p.read_text(encoding="utf-8")
    ]
    assert importers == ["llm/adapter.py"]
