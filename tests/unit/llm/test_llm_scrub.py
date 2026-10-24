# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Scrubber: secrets/canaries block, card/Aadhaar/OTP redact, names/emails pass."""

from __future__ import annotations

import pytest

from src.llm.scrub import Scrubber, ScrubBlocked


@pytest.mark.parametrize(
    "text",
    [
        "sk-proj-abcdefghijklmnopqrstuvwx",  # pragma: allowlist secret
        "AIza" + "A" * 35,
        "ghp_" + "a" * 36,
        "AKIA" + "A" * 16,
        "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4",
        "secret_" + "a" * 40,
        "client_secret: abcdefghijklmnop",
        "1//0gAbCdEfGhIjKlMnOpQrStUv",  # pragma: allowlist secret
        "sk\u200b-abcdefghijklmnopqrstuvwx",  # zero-width evasion  # pragma: allowlist secret
        "ｓｋ-abcdefghijklmnopqrstuvwx",  # fullwidth evasion
    ],
)
def test_secrets_block(text: str) -> None:
    with pytest.raises(ScrubBlocked) as ei:
        Scrubber().scrub(text)
    assert text not in str(ei.value)


def test_canaries_and_literal_secrets_block_but_short_ones_are_ignored() -> None:
    s = Scrubber(canaries=["CANARY-7f3a9c1e"], secrets=["tok_live_123456789", "dev"])
    with pytest.raises(ScrubBlocked) as ei:
        s.check("... CANARY-7f3a9c1e ...")
    assert ei.value.categories == ("canary",)
    with pytest.raises(ScrubBlocked):
        s.check("x tok_live_123456789 y")
    s.check("a dev environment")  # 'dev' is too short to be a meaningful secret


def test_redactions() -> None:
    r = Scrubber().scrub(
        "card 4111-1111-1111-1111 aadhaar 2345 6789 0123 your OTP is 482913 call me"
    )
    assert "4111" not in r.text and "2345" not in r.text and "482913" not in r.text
    assert sorted(r.redactions) == ["aadhaar", "card", "otp"]
    assert "call me" in r.text


def test_non_luhn_digit_runs_and_plain_numbers_untouched() -> None:
    text = "order 1234567890123456 total 4999 on 2026-10-25, meet at 3pm"
    assert Scrubber().scrub(text).text == text


def test_names_emails_places_are_not_blocked() -> None:
    text = "Ask Priya Sharma (priya@example.com) about the Mumbai trip at Google."
    r = Scrubber().scrub(text)
    assert r.text == text and r.redactions == []
