# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Obfuscation-tolerant canary detector (P4.4). Pure, synchronous, no I/O.

Every text is reduced to a *skeleton*: Unicode tag characters mapped back to
ASCII, NFKD, case folded, common Cyrillic/Greek look-alikes mapped to Latin,
everything but `[a-z0-9]` dropped (spaces, punctuation, zero-width, bidi,
combining marks), then look-alike digits folded onto letters (0->o, 1/i->l,
3->e, ...). So spaced, dotted, zero-width-split, case-changed, full-width,
accented, homoglyph and leetspeak forms all collapse onto the same string.

A planted canary is random, so ANY 10-character run of its skeleton (also of
the reversed and the ROT13 value) is treated as a leak: half or a third of a
value is caught, and a value spread over several outbound messages is caught
by the guard's rolling tail (`src.canary.guard`). The chance that unrelated
text contains one such run is about 2e-7 per 4 KB message (estimate;
measured 0/1500).
Operator-configured literals (`LLM_CANARIES`) are not random, so only their
whole skeleton counts. Letter-substitution forms are precomputed on the
canary side: every ROT-N (also of the reversed value) and Atbash. For the guard's stream check, `scattered` also flags
two different 6-character pieces of the same value (pieces sent out of order).

Decoded candidates are scanned too (two levels deep): HTML entities, percent
and backslash escapes; base64 / base64url (also with the whitespace removed);
hex (with `0x`, `\\x`, `%`, space, colon or dash separators); base32; decimal,
octal and binary character codes.

Fail closed: text over `MAX_SCAN_CHARS` is a hit (`oversize`). Top-level
decoding is linear in the text; nested decoding (only of mostly printable
decoded text) is capped by `MAX_RUNS` / `MAX_NESTED_CHARS`, over which the text
is a hit (`too_complex`). The detector is
a tripwire, not a boundary: a model that invents its own cipher, or leaks
pieces shorter than 6 characters, or spreads pieces further apart than the
guard's stream window, is not detected. The real bounds are policy, approvals
and the egress allowlist.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import html
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from urllib.parse import unquote

FRAGMENT_CHARS = 10
PIECE_CHARS = 6  # stream check only (`scattered`)
MIN_LITERAL_CHARS = 8
MAX_SCAN_CHARS = 131_072
MAX_RUNS = 1024  # decoded runs below the top level
MAX_NESTED_CHARS = 1_000_000  # decoded characters below the top level
MAX_DEPTH = 2

_TAGS = {cp: cp - 0xE0000 for cp in range(0xE0020, 0xE007F)}
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic (lower case; input is case folded first)
        "а": "a",
        "в": "b",
        "е": "e",
        "ё": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "ԁ": "d",
        "ԛ": "q",
        "ԝ": "w",
        "ɡ": "g",
        # Greek
        "α": "a",
        "β": "b",
        "ε": "e",
        "η": "n",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "ω": "w",
        "ζ": "z",
        # Latin extras that survive NFKD
        "ı": "i",
        "ł": "l",
        "ø": "o",
        "đ": "d",
        "ħ": "h",
    }
)
_LEET = str.maketrans("01i345789", "olleastbg")
_KEEP = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_HEX_SEPS = re.compile(r"(?i)0x|\\x|%|[\s:,;.\-]")
_HEX_RUN = re.compile(r"[0-9A-Fa-f]{20,}")
_B32_RUN = re.compile(r"[A-Za-z2-7]{16,}=*")
_CODES_RUN = re.compile(r"(?:\b\d{2,3}\b[\s,;]+){9,}\b\d{2,3}\b")
_BIN_RUN = re.compile(r"(?:\b[01]{7,8}\b[\s,;]*){10,}")
_OCT_ESC_RUN = re.compile(r"(?:\\0?[0-7]{3}){10,}")
_U_ESC = re.compile(r"\\u([0-9A-Fa-f]{4})|\\x([0-9A-Fa-f]{2})")
_WS = re.compile(r"\s+")


def skeleton(text: str) -> str:
    """The canonical comparison form (see the module docstring)."""
    t = unicodedata.normalize("NFKD", text.translate(_TAGS)).casefold()
    t = t.translate(_CONFUSABLES)
    return "".join(c for c in t if c in _KEEP).translate(_LEET)


def _rot13(text: str) -> str:
    return codecs.encode(text, "rot13")


_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ATBASH = str.maketrans(_LOWER, _LOWER[::-1])


def _rot(text: str, n: int) -> str:
    return text.translate(str.maketrans(_LOWER, _LOWER[n:] + _LOWER[:n]))


def _forms(value: str) -> list[tuple[str, str, bool]]:
    """(suffix, transformed value, primary) for every letter-substitution form
    a model can apply without a tool: identity, reversed, every ROT-N (also of
    the reversed value) and Atbash (also reversed). Only the primary forms
    (identity, reversed, ROT13) feed the stream piece check, so its false-hit
    rate stays unchanged; all forms feed the 10-character fragment check."""
    v = value.casefold()
    out = [("", v, True), ("+reversed", v[::-1], True)]
    for n in range(1, 26):
        out.append((f"+rot{n}", _rot(v, n), n == 13))
        out.append((f"+reversed+rot{n}", _rot(v[::-1], n), False))
    out.append(("+atbash", v.translate(_ATBASH), False))
    out.append(("+reversed+atbash", v[::-1].translate(_ATBASH), False))
    return out


@dataclass(frozen=True)
class CanaryHit:
    """Which canary (site label, never the value) and how it was encoded."""

    site: str
    encoding: str


class _TooComplex(Exception):
    pass


def _bytes_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="ignore").replace("\x00", "")


def _from_codes(codes: Iterable[int]) -> str:
    return "".join(chr(c) for c in codes if 32 <= c < 127)


def _mostly_printable(text: str) -> bool:
    """Decoded garbage (a base64 decode of ordinary words) is not decoded again."""
    if not text:
        return False
    ok = sum(1 for c in text if c.isprintable() or c in "\n\t")
    return ok >= 0.85 * len(text)


def _unescape(text: str) -> str:
    def esc(m: re.Match[str]) -> str:
        return chr(int(m.group(1) or m.group(2), 16))

    out = text
    for _ in range(2):
        out = _U_ESC.sub(esc, html.unescape(unquote(out)))
    return out


class CanaryDetector:
    """`entries`: (site label, value, fragments). `fragments=True` for random
    planted values (any 10-char run counts), False for configured literals
    (whole value only; < 8 skeleton chars are ignored, as in the scrubber)."""

    def __init__(self, entries: Iterable[tuple[str, str, bool]]) -> None:
        self._grams: dict[str, tuple[str, str]] = {}
        # Short pieces for the stream check: gram -> [(site, suffix, offset)].
        self._pieces: dict[str, list[tuple[str, str, int]]] = {}
        self._whole: list[tuple[str, str, str]] = []
        for site, value, fragments in entries:
            if not isinstance(value, str):
                continue
            for suffix, raw, primary in _forms(value):
                form = skeleton(raw)
                if len(form) < MIN_LITERAL_CHARS:
                    continue
                if fragments and len(form) >= FRAGMENT_CHARS:
                    for i in range(len(form) - FRAGMENT_CHARS + 1):
                        self._grams.setdefault(form[i : i + FRAGMENT_CHARS], (site, suffix))
                    if not primary:
                        continue
                    for i in range(len(form) - PIECE_CHARS + 1):
                        self._pieces.setdefault(form[i : i + PIECE_CHARS], []).append(
                            (site, suffix, i)
                        )
                else:
                    self._whole.append((form, site, suffix))

    @property
    def empty(self) -> bool:
        return not self._grams and not self._whole

    # ------------------------------------------------------------ matching

    def scattered(self, sk: str, *, new_from: int) -> tuple[str, str] | None:
        """Stream check for a canary cut into short pieces and sent out of
        order: two non-overlapping `PIECE_CHARS` pieces of the SAME planted
        value (different offsets) inside `sk`, at least one of them at or after
        `new_from` (the part not yet checked). Chance on unrelated text: about
        (n * 27 / 25**6)**2 per value and variant (~1e-7 for 2 KB)."""
        seen: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for i in range(len(sk) - PIECE_CHARS + 1):
            for site, suffix, off in self._pieces.get(sk[i : i + PIECE_CHARS], ()):
                prior = seen.setdefault((site, suffix), [])
                for pos, prev_off in prior:
                    if (
                        abs(off - prev_off) >= PIECE_CHARS
                        and i - pos >= PIECE_CHARS
                        and i >= new_from
                    ):
                        return site, suffix
                prior.append((i, off))
        return None

    def match_skeleton(self, sk: str) -> tuple[str, str] | None:
        """(site, suffix) of the first canary run found in a skeleton."""
        grams = self._grams
        if grams:
            for i in range(len(sk) - FRAGMENT_CHARS + 1):
                got = grams.get(sk[i : i + FRAGMENT_CHARS])
                if got is not None:
                    return got
        for form, site, suffix in self._whole:
            if form in sk:
                return site, suffix
        return None

    def detect(self, text: str) -> CanaryHit | None:
        """First hit in `text` or any decoded form of it; None if clean."""
        if not isinstance(text, str):
            text = str(text)
        if len(text) > MAX_SCAN_CHARS:
            return CanaryHit("unknown", "oversize")
        if self.empty:
            return None
        seen: set[str] = set()
        try:
            for encoding, candidate in self._candidates(text, "plain", 0, [0, 0]):
                if candidate in seen:
                    continue
                seen.add(candidate)
                got = self.match_skeleton(skeleton(candidate))
                if got is not None:
                    return CanaryHit(got[0], encoding + got[1])
        except _TooComplex:
            return CanaryHit("unknown", "too_complex")
        return None

    # ---------------------------------------------------------- candidates

    def _candidates(
        self, text: str, label: str, depth: int, runs: list[int]
    ) -> Iterator[tuple[str, str]]:
        yield label, text
        if depth >= MAX_DEPTH:
            return
        prefix = "" if label == "plain" else label + ">"
        unescaped = _unescape(text)
        if unescaped != text:
            yield prefix + "escaped", unescaped
        sources = {text, unescaped}
        if depth == 0:
            # "aGVs bG8g": base64/hex split by spaces or line breaks.
            sources |= {_WS.sub("", s) for s in sources}
        for decoded_label, decoded in self._decoded(sources, runs if depth else None):
            if _mostly_printable(decoded):
                yield from self._candidates(decoded, prefix + decoded_label, depth + 1, runs)
            else:
                yield prefix + decoded_label, decoded  # scanned, not decoded further

    def _decoded(self, sources: set[str], runs: list[int] | None) -> Iterator[tuple[str, str]]:
        """`runs` = [runs, chars] budget for nested decoding (None at the top
        level, whose cost is linear in the already length-capped text)."""

        def count(size: int = 0) -> None:
            if runs is None:
                return
            runs[0] += 1
            runs[1] += size
            if runs[0] > MAX_RUNS or runs[1] > MAX_NESTED_CHARS:
                raise _TooComplex

        for src in sorted(sources):
            for m in _B64_RUN.finditer(src):
                count(len(m.group()) * 4)
                run = m.group().rstrip("=").replace("-", "+").replace("_", "/")
                for off in range(4):
                    part = run[off:]
                    part = part[: len(part) // 4 * 4]
                    if len(part) < 12:
                        continue
                    try:
                        yield "base64", _bytes_text(base64.b64decode(part))
                    except (binascii.Error, ValueError):
                        continue
            for m in _B32_RUN.finditer(src):
                count(len(m.group()) * 8)
                run = m.group().rstrip("=").upper()
                for off in range(8):
                    part = run[off:]
                    part = part[: len(part) // 8 * 8]
                    if len(part) < 16:
                        continue
                    try:
                        yield "base32", _bytes_text(base64.b32decode(part))
                    except (binascii.Error, ValueError):
                        continue
            for m in _HEX_RUN.finditer(_HEX_SEPS.sub("", src)):
                count(len(m.group()))
                run = m.group()
                for off in range(2):
                    part = run[off:]
                    part = part[: len(part) // 2 * 2]
                    try:
                        yield "hex", _bytes_text(bytes.fromhex(part))
                    except ValueError:
                        continue
            for m in _CODES_RUN.finditer(src):
                count(len(m.group()))
                digits = re.findall(r"\d{2,3}", m.group())
                yield "charcodes", _from_codes(int(x) for x in digits)
                if all(set(x) <= set("01234567") for x in digits):
                    yield "octal", _from_codes(int(x, 8) for x in digits)
            for m in _BIN_RUN.finditer(src):
                count(len(m.group()))
                bits = re.findall(r"[01]{7,8}", m.group())
                yield "binary", _from_codes(int(x, 2) for x in bits)
            for m in _OCT_ESC_RUN.finditer(src):
                count(len(m.group()))
                octs = re.findall(r"\\0?([0-7]{3})", m.group())
                yield "octal", _from_codes(int(x, 8) for x in octs)
