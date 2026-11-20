# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Unit tests for `src/personality/agent_character.py`: character card
loading, the friction checker, and PSA-1's "no code path writes the
character card" CI check (an AST scan of `src/`, same technique as
`tests/eval/invariants/test_inv1_single_egress_point.py`).
"""

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.personality.agent_character import (
    CHARACTER_CARD_PATH,
    AgentStance,
    check_friction,
    load_character_card,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
_WRITE_METHODS = {"write_text", "write_bytes", "write", "writelines"}


def test_character_card_file_exists():
    assert CHARACTER_CARD_PATH.exists()


def test_load_character_card_reads_file():
    content = load_character_card()

    assert content == CHARACTER_CARD_PATH.read_text(encoding="utf-8")


def test_load_character_card_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.md"

    with pytest.raises(FileNotFoundError):
        load_character_card(missing)


def _stance(topic: str, position: str, strength: str) -> AgentStance:
    return AgentStance(
        id=uuid.uuid4(),
        topic=topic,
        position=position,
        reasoning="past incidents",
        taken_at=datetime.now(UTC),
        session_id=uuid.uuid4(),
        strength=strength,
    )


def test_friction_checker_flags_conflicting_held_stance():
    stance = _stance(
        topic="scope creep",
        position="push back on scope creep even under deadline pressure",
        strength="firm",
    )

    conflicts = check_friction(
        request="let's just add some scope creep to this sprint, we need it",
        user_goals=[],
        prior_stances=[stance],
    )

    assert any(c.kind == "stance" for c in conflicts)


def test_friction_checker_ignores_tentative_stance():
    stance = _stance(topic="scope creep", position="lean towards avoiding it", strength="tentative")

    conflicts = check_friction(
        request="let's add some scope creep to this sprint",
        user_goals=[],
        prior_stances=[stance],
    )

    assert conflicts == []


def test_friction_checker_flags_goal_conflict():
    conflicts = check_friction(
        request="I'm going to stop exercising, it's not worth it",
        user_goals=["exercise three times a week"],
        prior_stances=[],
    )

    assert any(c.kind == "goal" for c in conflicts)


def test_friction_checker_no_conflict_when_none_present():
    conflicts = check_friction(
        request="what's a good recipe for lentil soup",
        user_goals=["exercise three times a week"],
        prior_stances=[],
    )

    assert conflicts == []


def _open_call_is_write(node: ast.Call) -> bool:
    if not (isinstance(node.func, ast.Name) and node.func.id == "open"):
        return False
    mode_arg = node.args[1] if len(node.args) >= 2 else None
    if mode_arg is None:
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_arg = kw.value
    if mode_arg is None:
        return False
    return (
        isinstance(mode_arg, ast.Constant)
        and isinstance(mode_arg.value, str)
        and any(flag in mode_arg.value for flag in "wax")
    )


def _references_character_card(node: ast.AST) -> bool:
    """True if `node`'s subtree mentions the literal path or the exported
    `CHARACTER_CARD_PATH` name -- catches both a hardcoded path and a write
    routed through the module constant.
    """
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "character_card.md" in n.value
        ):
            return True
        if isinstance(n, ast.Name) and n.id == "CHARACTER_CARD_PATH":
            return True
        if isinstance(n, ast.Attribute) and n.attr == "CHARACTER_CARD_PATH":
            return True
    return False


def test_no_code_path_writes_the_character_card():
    """PSA-1 CI check: `character_card.md` is hand-authored only."""
    hits: list[tuple[str, int]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_write_call = _open_call_is_write(node) or (
                isinstance(node.func, ast.Attribute) and node.func.attr in _WRITE_METHODS
            )
            if is_write_call and _references_character_card(node):
                hits.append((str(path.relative_to(SRC_ROOT.parent)), node.lineno))

    assert hits == [], f"PSA-1: found code path(s) writing character_card.md: {hits}"
