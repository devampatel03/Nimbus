# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

# ruff: noqa: C408

"""P3.9: deadline resolution is code, in Asia/Kolkata, relative to the message
time. Ambiguous or forged wording stays UNRESOLVED (never guessed)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from src.memory.commitments import (
    Commitment,
    has_commitment_cue,
    normalise,
    resolve_due,
)

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)  # Thursday 11:30 IST


def test_friday_resolves_to_absolute_ist_instant() -> None:
    d = resolve_due("by Friday", body="I'll send the deck by Friday", event_time=NOW)
    assert d.state == "resolved"
    assert d.at == datetime(2026, 10, 30, 3, 30, tzinfo=UTC)  # Fri 30 Oct 09:00 IST


def test_explicit_time_resolves() -> None:
    d = resolve_due("Friday 5pm", body="send it Friday 5pm", event_time=NOW)
    assert d.state == "resolved" and d.at == datetime(2026, 10, 30, 11, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "phrase",
    ["5/6", "05/06/2027", "thursday", "next friday", "soon", "next week", "eod", "yesterday", "5"],
)
def test_ambiguous_or_unknown_stays_unresolved(phrase: str) -> None:
    d = resolve_due(phrase, body=f"I'll do it {phrase}", event_time=NOW)
    assert d.state == "unresolved" and d.at is None


def test_phrase_not_in_message_is_forged() -> None:
    d = resolve_due("tomorrow", body="I'll send the deck by Friday", event_time=NOW)
    assert d.state == "unresolved" and d.at is None


@pytest.mark.parametrize("phrase", [None, "", "   "])
def test_no_phrase_is_none(phrase: str | None) -> None:
    assert resolve_due(phrase, body="I'll send the deck", event_time=NOW).state == "none"


def test_relative_to_event_time_not_wall_clock() -> None:
    later = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
    d = resolve_due("Friday", body="by Friday", event_time=later)
    assert d.at == datetime(2026, 11, 6, 3, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "I'll send the report by Friday",
        "I will email Priya",
        "I'm going to call him",
        "I promise it",
    ],
)
def test_cue_matches(text: str) -> None:
    assert has_commitment_cue(text)


@pytest.mark.parametrize("text", ["ok thanks", "what is the weather", "she'll send it"])
def test_cue_rejects(text: str) -> None:
    assert not has_commitment_cue(text)


def test_normalise_ignores_case_punctuation_spacing() -> None:
    assert normalise("Send  the Deck!") == normalise("send the deck")
    assert normalise("send the deck") != normalise("send the slides")


def test_preview_is_built_from_the_row_and_asks_when_unresolved() -> None:
    def row(**kw: object) -> Commitment:
        base: dict[str, object] = dict(
            id=uuid.uuid4(), key=uuid.uuid4(), version=1, status="candidate",
            description="send the deck", due_at=None, due_state="unresolved", due_phrase="5/6",
            lane="trusted", source_event_id=uuid.uuid4(), confidence=0.9,
        )  # fmt: skip
        base.update(kw)
        return Commitment(**base)  # type: ignore[arg-type]

    assert "NOT SET" in row().preview()
    assert "UNTRUSTED" in row(lane="untrusted").preview()
    assert (
        "Fri 30 Oct 2026, 9:00 AM IST"
        in row(due_at=datetime(2026, 10, 30, 3, 30, tzinfo=UTC), due_state="resolved").preview()
    )
