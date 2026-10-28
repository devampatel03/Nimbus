# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Cross-turn taint carry (ADR-0010 item 5)."""

from __future__ import annotations

from typing import Any

import pytest

from src.policy import TaintCarry, carry_turns_for


def test_starts_clean() -> None:
    assert TaintCarry().carried is False


def test_tainted_turn_carries_for_three_kept_clean_turns() -> None:
    c = TaintCarry(3)
    c.after_turn(tainted=True, kept=True)
    seen = []
    for _ in range(4):
        seen.append(c.carried)
        c.after_turn(tainted=False, kept=True)
    assert seen == [True, True, True, False]
    assert c.carried is False


def test_unkept_clean_turns_do_not_consume_the_carry() -> None:
    c = TaintCarry(2)
    c.after_turn(tainted=True, kept=True)
    for _ in range(10):
        c.after_turn(tainted=False, kept=False)
    assert c.carried is True


def test_retaint_rearms() -> None:
    c = TaintCarry(2)
    c.after_turn(tainted=True, kept=True)
    c.after_turn(tainted=False, kept=True)
    c.after_turn(tainted=True, kept=True)
    c.after_turn(tainted=False, kept=True)
    assert c.carried is True


def test_failed_turn_arms_the_carry() -> None:
    c = TaintCarry(1)
    c.after_failed_turn()
    assert c.carried is True


@pytest.mark.parametrize("bad", [None, 0, 1, "no", object()])
def test_non_bool_taint_fails_closed(bad: Any) -> None:
    c = TaintCarry(1)
    c.after_turn(tainted=bad, kept=True)
    assert c.carried is True


def test_non_bool_kept_does_not_consume() -> None:
    c = TaintCarry(1)
    c.after_turn(tainted=True, kept=True)
    c.after_turn(tainted=False, kept=1)  # type: ignore[arg-type]
    assert c.carried is True


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "3"])
def test_bad_turns_rejected(bad: Any) -> None:
    with pytest.raises(ValueError):
        TaintCarry(bad)


@pytest.mark.parametrize(("hist", "want"), [(0, 3), (2, 3), (6, 3), (7, 4), (8, 4), (20, 10)])
def test_carry_covers_the_history_window(hist: int, want: int) -> None:
    assert carry_turns_for(hist) == want


@pytest.mark.parametrize("bad", [-1, 2.0, None, True])
def test_carry_turns_for_rejects_bad(bad: Any) -> None:
    with pytest.raises(ValueError):
        carry_turns_for(bad)


def test_state_cannot_be_reset_from_outside() -> None:
    c = TaintCarry(1)
    c.after_turn(tainted=True, kept=True)
    with pytest.raises(AttributeError):
        c.carried = False  # type: ignore[misc]
    with pytest.raises(AttributeError):
        c.reset = lambda: None  # type: ignore[attr-defined]
