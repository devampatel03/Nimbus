# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""R9 / M-3: real JSON through .claude/hooks/protect-files.sh; assert exit code 2.

Bash redirects / `python -c` can bypass a Write/Edit hook (ADR-0015 accepts
this; documented in the hook). These tests cover path-normalisation evasions.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "protect-files.sh"
BASH = shutil.which("bash")
JQ = shutil.which("jq")

BLOCKED = [
    "personality/character_card.md",
    "personality/character_card.sha256",
    "/repo/personality/character_card.md",
    "C:\\repo\\personality\\character_card.md",
    "Personality\\Character_Card.md",
    "personality/CHARACTER_CARD.MD",
    "personality/CHARACTER_CARD.SHA256",
    "./personality/character_card.md",
    "personality//character_card.md",
    "personality/./character_card.md",
    "personality/sub/../character_card.md",
    "other/../personality/character_card.md",
    "personality/character_card.md.",
    "personality/character_card.md ",
    "personality/character_card.md::$DATA",
    "personality/character_card.md:stream",
    "personality/..x/../character_card.md",
    "personality/..../../character_card.md",
    "personality/x/.../../character_card.md",
    "personality/CHARAC~1.MD",
    "personality/CHARAC~2.SHA",
    "PERSON~1/character_card.md",
]
ALLOWED = [
    "src/personality/agent_character.py",
    "personality/notes.txt",
    "tests/unit/test_r9_character_card.py",
    "docs/character_card.md",
]


def _run(path: str) -> int:
    assert BASH and JQ, "bash and jq are required for the R9 hook tests"
    payload = json.dumps({"tool_input": {"file_path": path}})
    return subprocess.run(
        [BASH, str(HOOK)], input=payload, text=True, capture_output=True, timeout=30, check=False
    ).returncode


@pytest.mark.parametrize("path", BLOCKED)
def test_hook_blocks_card_and_pin_variants(path):
    assert _run(path) == 2


@pytest.mark.parametrize("path", ALLOWED)
def test_hook_allows_unrelated_paths(path):
    assert _run(path) == 0


def test_hook_documents_bash_bypass_limit():
    assert "Bash" in HOOK.read_text(encoding="utf-8") and "ADR-0015" in HOOK.read_text(
        encoding="utf-8"
    )
