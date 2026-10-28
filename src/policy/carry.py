# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Cross-turn taint carry (ADR-0010 item 5: "the turn plus 3 turns").

A tainted turn's reply is kept in the conversation window, so text steered by
injected data re-enters later turns as if it were trusted. The carry keeps
those later turns tainted:

- a tainted turn (or a turn that failed, whose taint is unknown) re-arms the
  carry to `turns` clean turns;
- a clean turn uses up one carried turn only if it was kept in the history
  window (each kept turn pushes two messages, so after `turns` kept turns the
  tainted reply has been trimmed when `turns >= history_messages / 2`);
- a clean turn that was not kept consumes nothing (conservative).

Known limits (residual risk, reviewer MED-1 on PR #31; bounded, not closed):

- NOT transitive. A carried turn reports only its OWN taint, so it does not
  re-arm the carry (otherwise the carry would never end). A reply written in a
  carried turn can repeat or paraphrase the injected instruction, and that
  reply stays in the history window after the original tainted reply has been
  trimmed and the carry has expired. Later turns then see the steered text as
  untainted history. Writes still need the model to act on it, and every
  external effect and destructive action needs approval regardless of taint
  (ADR-0009); only untainted WRITE_INTERNAL is exposed by this gap.
- The window is about `ceil(history_messages / 2)` kept turns (minimum 3). It
  assumes each kept turn adds exactly two messages (user text + final reply,
  `src.app.pipeline._remember`); if that changes, `carry_turns_for` must too.
- In-process only: a restart clears the carry, which is consistent only
  because the history window is in-process too.

Pure and deterministic; the conversation worker owns one instance.
"""

from __future__ import annotations

ADR_0010_CARRY_TURNS = 3


def carry_turns_for(history_messages: int) -> int:
    """At least ADR-0010's 3 turns, and long enough that a tainted reply has
    left a `history_messages` window before the carry ends."""
    if type(history_messages) is not int or history_messages < 0:
        raise ValueError("history_messages must be a non-negative int")
    return max(ADR_0010_CARRY_TURNS, -(-history_messages // 2))


class TaintCarry:
    __slots__ = ("_remaining", "_turns")

    def __init__(self, turns: int = ADR_0010_CARRY_TURNS) -> None:
        if type(turns) is not int or turns < 1:
            raise ValueError("turns must be a positive int")
        self._turns = turns
        self._remaining = 0

    @property
    def carried(self) -> bool:
        """True while an earlier tainted turn still taints the next turn."""
        return self._remaining > 0

    def after_turn(self, *, tainted: bool, kept: bool) -> None:
        """Record a finished turn. `tainted` is the turn's OWN taint (not the
        carried one, or the carry would never end); non-bool fails closed."""
        if tainted is not False:
            self._remaining = self._turns
        elif kept is True and self._remaining > 0:
            self._remaining -= 1

    def after_failed_turn(self) -> None:
        """A turn that raised: its taint is unknown, so treat it as tainted."""
        self._remaining = self._turns
