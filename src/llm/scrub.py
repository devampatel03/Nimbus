# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Outbound text scrubber (INV-7, R4). Deterministic, dependency-free.

Posture (TARGET_DESIGN section 3, ADR-0003): the provider may see ordinary
personal text (names, e-mails, places), so those are NOT blocked here; the old
`pii_detector` name/address heuristics are not carried over. What never leaves:

* BLOCK (whole call refused): API keys / tokens / private keys / bearer
  credentials, configured canary strings, and the provider key itself.
* REDACT in place: payment-card numbers (Luhn-valid), Aadhaar-shaped numbers,
  and OTP-like codes next to an OTP cue word.

Findings carry only a category, never the matched text (INV-7).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import unquote

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b‌‍⁠﻿­"))

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("telegram_token", re.compile(r"(?:(?<![A-Za-z0-9])|(?i:bot))\d{8,12}:[A-Za-z0-9_-]{35,}")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_token", re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}|\b1//[A-Za-z0-9_-]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("notion_token", re.compile(r"\b(?:secret_|ntn_)[A-Za-z0-9]{30,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|client[_-]?secret)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{12,}"
        ),
    ),
)

_CARD_RE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_AADHAAR_RE = re.compile(r"(?<![\d-])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d-])")
_OTP_RE = re.compile(
    r"(?i)\b(otp|one[- ]time (?:password|code|pin)|verification code|passcode|security code)"
    r"([^\d\n]{0,24})(\d{4,8})\b"
)


class ScrubBlocked(Exception):
    """A secret/canary was found. `categories` are fixed codes, no text."""

    def __init__(self, categories: tuple[str, ...]) -> None:
        self.categories = categories
        super().__init__("scrub blocked categories=" + ",".join(categories))


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class ScrubResult:
    text: str
    redactions: list[str] = field(default_factory=list)


class Scrubber:
    def __init__(self, canaries: Iterable[str] = (), secrets: Iterable[str] = ()) -> None:
        """`canaries`: planted tripwire strings. `secrets`: literal credential
        values (e.g. the provider API key). Both block; empty strings and
        very short secrets (e.g. the dev placeholder) are ignored."""
        self._literals = tuple(s for s in (*canaries, *secrets) if len(s) >= 8)

    def check(self, text: str) -> None:
        """Raise `ScrubBlocked` if `text` holds a secret or canary."""
        variants = {text, unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)}
        variants |= {
            unicodedata.normalize("NFKC", unquote(v)).translate(_ZERO_WIDTH) for v in variants
        }
        found: list[str] = []
        for variant in variants:
            for category, pattern in _SECRET_PATTERNS:
                if category not in found and pattern.search(variant):
                    found.append(category)
            if "canary" not in found and any(lit in variant for lit in self._literals):
                found.append("canary")
        if found:
            raise ScrubBlocked(tuple(found))

    def scrub(self, text: str) -> ScrubResult:
        self.check(text)
        result = ScrubResult(text)

        def card(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group())
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                result.redactions.append("card")
                return "[REDACTED:card]"
            return m.group()

        def aadhaar(m: re.Match[str]) -> str:
            result.redactions.append("aadhaar")
            return "[REDACTED:aadhaar]"

        def otp(m: re.Match[str]) -> str:
            result.redactions.append("otp")
            return f"{m.group(1)}{m.group(2)}[REDACTED:otp]"

        out = _CARD_RE.sub(card, result.text)
        out = _AADHAAR_RE.sub(aadhaar, out)
        out = _OTP_RE.sub(otp, out)
        result.text = out
        return result
