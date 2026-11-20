# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""#39 LOWs A-C: calendar `HandleBook` rollover.

A. A fixed (test) nonce must not survive a rollover: after the list counter
   wraps, `h<nonce>1_1` would otherwise be re-issued for a different event, so
   a handle the model saw before could silently name another event.
B. A redraw must never repeat a nonce already used in this process.
C. The rollover clears the id map (no stale entries, bounded memory).
"""

from __future__ import annotations

import pytest

from src.orchestrator.dispatch import ToolError
from src.tools.integrations import google_calendar as gc
from src.tools.integrations.google_calendar import HANDLE_RE, MAX_LISTS, HandleBook


def _roll(book: HandleBook, eid: str = "evt_filler_0001") -> None:
    for _ in range(MAX_LISTS):
        book.issue([eid])


@pytest.mark.parametrize("nonce", ["123456", None])
def test_pre_rollover_handle_never_names_a_post_rollover_event(nonce: str | None) -> None:
    book = HandleBook(nonce)
    old = book.issue(["evt_victim_0001"])[0]
    for _ in range(MAX_LISTS - 1):
        book.issue(["evt_filler_0001"])
    new = book.issue(["evt_attacker_01"])[0]  # first list after the wrap
    assert HANDLE_RE.fullmatch(new)
    assert new != old
    with pytest.raises(ToolError):
        book.resolve(old)  # stale: fails closed, never resolves to the new event
    assert book.resolve(new) == "evt_attacker_01"


def test_fixed_nonce_is_only_the_first_nonce() -> None:
    book = HandleBook("123456")
    assert book.issue(["evt_a"])[0].startswith("h123456")
    _roll(book)
    assert not book.issue(["evt_b"])[0].startswith("h123456")


def test_redraw_never_repeats_a_used_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    draws = iter(["111111", "111111", "222222", "111111", "222222", "333333"])
    monkeypatch.setattr(gc.secrets, "randbelow", lambda n: int(next(draws)))
    book = HandleBook()  # draws 111111
    seen = {book.issue(["e"])[0][1:7]}
    _roll(book)  # must skip the repeated 111111 -> 222222
    seen.add(book.issue(["e"])[0][1:7])
    _roll(book)  # must skip 111111 and 222222 -> 333333
    seen.add(book.issue(["e"])[0][1:7])
    assert seen == {"111111", "222222", "333333"}


def test_redraw_gives_up_closed_if_it_cannot_find_a_fresh_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc.secrets, "randbelow", lambda n: 424242)
    book = HandleBook()
    book.issue(["e"])
    for _ in range(MAX_LISTS - 1):
        book.issue(["e"])
    with pytest.raises(ToolError):
        book.issue(["e"])  # no fresh nonce: refuse to issue rather than reuse


def test_rollover_clears_the_map() -> None:
    book = HandleBook("123456")
    for i in range(MAX_LISTS):
        book.issue([f"evt_{i:04d}_a", f"evt_{i:04d}_b"])
    assert len(book._ids) == gc.MAX_HANDLES
    fresh = book.issue(["evt_new"])
    assert dict(book._ids) == {fresh[0]: "evt_new"}
