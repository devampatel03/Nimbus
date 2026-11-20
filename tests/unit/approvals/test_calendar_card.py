# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P3.5: a calendar approval card shows start/end as sent (ISO) AND in IST,
computed by code from the payload (never model prose)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.approvals.card import render_card


def card(payload: dict[str, str]) -> str:
    now = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
    return render_card(
        tool="calendar_create",
        action="call",
        payload=payload,
        expires_at=now + timedelta(minutes=10),
        now=now,
        offset_minutes=330,
        step_up=True,
        rule_id="approval.write_external",
    )


def test_card_shows_iso_and_ist_for_start_and_end() -> None:
    text = card(
        {"title": "Dentist", "start": "2026-11-02T10:30:00Z", "end": "2026-11-02T11:30:00Z"}
    )
    assert "Start: 2026-11-02T10:30:00Z = 02 Nov 2026, 16:00:00 IST" in text
    assert "End: 2026-11-02T11:30:00Z = 02 Nov 2026, 17:00:00 IST" in text
    assert '"start": "2026-11-02T10:30:00Z"' in text  # the raw argument is still shown


def test_card_ignores_naive_or_non_time_values() -> None:
    text = card({"start": "tomorrow 4pm", "end": "2026-11-02T10:00:00"})
    assert "Start:" not in text and "End:" not in text
