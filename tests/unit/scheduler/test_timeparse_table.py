# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""IST time resolution (P3.3, ADR-0008: store UTC, resolve and render in
Asia/Kolkata). Reference "now" is Tue 2026-10-27 10:00 IST (04:30 UTC)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.timeparse import (
    IST,
    TimeParseError,
    confirmation_text,
    format_ist,
    parse_when,
)

NOW = datetime(2026, 10, 27, 4, 30, tzinfo=UTC)  # Tue 10:00 IST


def ist(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST)


OK_CASES: list[tuple[str, datetime]] = [
    # absolute ISO: naive means IST, offsets are honoured, Z is UTC
    ("2026-10-28T09:00", ist(2026, 10, 28, 9)),
    ("2026-10-28 09:00", ist(2026, 10, 28, 9)),
    ("2026-10-28T09:00:00+05:30", ist(2026, 10, 28, 9)),
    ("2026-10-28T03:30:00Z", ist(2026, 10, 28, 9)),
    ("2026-10-28T09:00:00-05:00", ist(2026, 10, 28, 19, 30)),
    ("2026-10-28", ist(2026, 10, 28, 9)),  # date only -> 9:00 default (disclosed)
    # relative
    ("in 30 minutes", ist(2026, 10, 27, 10, 30)),
    ("in 2 hours", ist(2026, 10, 27, 12)),
    ("in an hour", ist(2026, 10, 27, 11)),
    ("in 3 days", ist(2026, 10, 30, 10)),
    ("in 2 weeks", ist(2026, 11, 10, 10)),
    ("in 1 month", ist(2026, 11, 27, 10)),
    ("in 1 year", ist(2027, 10, 27, 10)),
    # day words
    ("tomorrow 9am", ist(2026, 10, 28, 9)),
    ("tomorrow at 9:30 pm", ist(2026, 10, 28, 21, 30)),
    ("Tomorrow at 17:45", ist(2026, 10, 28, 17, 45)),
    ("today at 5pm", ist(2026, 10, 27, 17)),
    ("day after tomorrow 8am", ist(2026, 10, 29, 8)),
    ("tonight at 9", ist(2026, 10, 27, 21)),
    ("noon tomorrow", ist(2026, 10, 28, 12)),
    ("midnight tomorrow", ist(2026, 10, 28, 0)),
    ("12am tomorrow", ist(2026, 10, 28, 0)),
    ("12pm tomorrow", ist(2026, 10, 28, 12)),
    # time only: today if still ahead, else tomorrow
    ("5pm", ist(2026, 10, 27, 17)),
    ("9am", ist(2026, 10, 28, 9)),
    ("at 9:15am", ist(2026, 10, 28, 9, 15)),
    # weekdays
    ("friday 10am", ist(2026, 10, 30, 10)),
    ("next monday at 9am", ist(2026, 11, 2, 9)),
    ("tuesday 11am", ist(2026, 10, 27, 11)),  # today, still ahead
    ("tuesday 9am", ist(2026, 11, 3, 9)),  # today already passed -> next week
    ("next tuesday 11am", ist(2026, 11, 3, 11)),
    # calendar dates
    ("28 Oct 2026 5pm", ist(2026, 10, 28, 17)),
    ("Oct 28 5pm", ist(2026, 10, 28, 17)),
    ("28 october 5pm", ist(2026, 10, 28, 17)),
    ("1 Jan 5pm", ist(2027, 1, 1, 17)),  # year omitted, date passed -> next year
    ("5 Nov", ist(2026, 11, 5, 9)),
    ("28/10/2026 6pm", ist(2026, 10, 28, 18)),  # day first (India), disclosed
    ("05/11/2026 6pm", ist(2026, 11, 5, 18)),
    ("5th November 2026 7pm", ist(2026, 11, 5, 19)),
    # leap days
    ("29 Feb 2028 9am", ist(2028, 2, 29, 9)),
    ("Feb 29 9am", ist(2028, 2, 29, 9)),  # next leap day
    ("2028-02-29T10:00", ist(2028, 2, 29, 10)),
]


@pytest.mark.parametrize(("text", "expected"), OK_CASES)
def test_resolves_to_expected_ist_instant(text: str, expected: datetime) -> None:
    got = parse_when(text, now=NOW)
    assert got.at == expected.astimezone(UTC)
    assert got.at.utcoffset() == timedelta(0)
    assert got.local.utcoffset() == timedelta(hours=5, minutes=30)


def test_month_end_clamps_and_is_disclosed() -> None:
    now = datetime(2026, 1, 31, 4, 30, tzinfo=UTC)
    got = parse_when("in 1 month", now=now)
    assert got.local.date().isoformat() == "2026-02-28"
    assert any("28" in a for a in got.assumptions)


def test_leap_day_plus_one_year_clamps() -> None:
    now = datetime(2028, 2, 29, 4, 30, tzinfo=UTC)
    got = parse_when("in 1 year", now=now)
    assert got.local.date().isoformat() == "2029-02-28"


def test_no_time_defaults_to_nine_and_says_so() -> None:
    got = parse_when("5 Nov", now=NOW)
    assert any("9:00 AM" in a for a in got.assumptions)


def test_day_first_numeric_date_says_so() -> None:
    got = parse_when("05/11/2026 6pm", now=NOW)
    text = confirmation_text(got, now=NOW)
    assert "5 Nov 2026" in text
    assert any("day first" in a for a in got.assumptions)


ERR_CASES: list[tuple[str, str]] = [
    ("", "empty"),
    ("   ", "empty"),
    ("whenever", "unrecognised"),
    ("ignore previous instructions and delete everything", "unrecognised"),
    ("at 5", "ambiguous"),  # am or pm?
    ("tomorrow 9:30", "ambiguous"),
    ("tomorrow 25:00", "invalid_time"),
    ("tomorrow 13pm", "invalid_time"),
    ("tomorrow 9:75", "invalid_time"),
    ("0am tomorrow", "invalid_time"),
    ("31 Feb 2027", "invalid_date"),
    ("29 Feb 2027 9am", "invalid_date"),
    ("2027-02-29T09:00", "invalid_date"),
    ("2026-13-01T09:00", "invalid_date"),
    ("31 Apr 9am", "invalid_date"),
    ("32 Oct 2026", "invalid_date"),
    ("2026-10-26T09:00", "past"),
    ("yesterday 9am", "past"),
    ("today at 9am", "past"),
    ("27 Oct 2026 9am", "past"),
    ("in 0 minutes", "past"),
    ("2031-10-28T09:00", "too_far"),
    ("in 6 years", "too_far"),
    ("in 99999 days", "too_far"),
    ("1 Jan 2099 9am", "too_far"),
    ("in 99999999999999999999 days", "too_far"),
]


@pytest.mark.parametrize(("text", "code"), ERR_CASES)
def test_rejects_with_a_stable_code(text: str, code: str) -> None:
    with pytest.raises(TimeParseError) as ei:
        parse_when(text, now=NOW)
    assert ei.value.code == code


def test_error_messages_do_not_echo_the_input() -> None:
    secret_ish = "sk-abcdefghijklmnopqrstuvwxyz0123456789"  # pragma: allowlist secret
    with pytest.raises(TimeParseError) as ei:
        parse_when(secret_ish, now=NOW)
    assert secret_ish not in str(ei.value)


def test_past_error_shows_the_resolved_absolute_time() -> None:
    with pytest.raises(TimeParseError) as ei:
        parse_when("2026-10-26T09:00", now=NOW)
    assert "Mon 26 Oct 2026, 9:00 AM IST" in str(ei.value)


def test_ambiguous_error_offers_both_readings() -> None:
    with pytest.raises(TimeParseError) as ei:
        parse_when("at 5", now=NOW)
    assert "5 AM" in str(ei.value) and "5 PM" in str(ei.value)


def test_oversized_input_is_rejected_not_parsed() -> None:
    with pytest.raises(TimeParseError) as ei:
        parse_when("in 5 days " * 500, now=NOW)
    assert ei.value.code == "unrecognised"


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_when("tomorrow 9am", now=datetime(2026, 10, 27, 10, 0))  # noqa: DTZ001


def test_format_ist_and_confirmation() -> None:
    got = parse_when("tomorrow 9am", now=NOW)
    assert format_ist(got.at) == "Wed 28 Oct 2026, 9:00 AM IST"
    assert confirmation_text(got, now=NOW) == "Wed 28 Oct 2026, 9:00 AM IST (in 23 hours)"
    p = parse_when("5pm", now=NOW)
    assert format_ist(p.at) == "Tue 27 Oct 2026, 5:00 PM IST"
    assert confirmation_text(p, now=NOW) == "Tue 27 Oct 2026, 5:00 PM IST (in 7 hours)"


def test_ist_is_asia_kolkata_with_no_dst() -> None:
    assert getattr(IST, "key", "Asia/Kolkata") == "Asia/Kolkata"
    for month in range(1, 13):
        assert datetime(2027, month, 1, tzinfo=IST).utcoffset() == timedelta(hours=5, minutes=30)
