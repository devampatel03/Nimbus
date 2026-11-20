# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Pure text hygiene for untrusted input. Total: never raises on any str/bytes.

Nothing here executes or renders content. We only remove characters that hide
or reorder text (zero-width, bidi overrides), NULs and C0/C1 controls (Postgres
rejects NUL in text; controls corrupt logs/terminals), replace lone surrogates,
normalise to NFC, and cap length. Newline and tab survive.
"""

from __future__ import annotations

import re
import unicodedata

# Soft hyphen, Arabic letter mark, Mongolian vowel separator, zero-width and
# bidi marks/embeddings/overrides/isolates, BOM, interlinear annotation, tags.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x061C, 0x061C),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0xE0000, 0xE007F),
)
# Built from code points so the source holds no invisible characters itself.
_INVISIBLE = re.compile(
    "[" + "".join(f"{chr(a)}-{chr(b)}" if a != b else chr(a) for a, b in _INVISIBLE_RANGES) + "]"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_CRLF = re.compile(r"\r\n?")
_REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


def clean_text(text: str, *, max_chars: int) -> tuple[str, bool]:
    """Return `(clean, truncated)`. Truncation happens on the raw input first
    (bounded work on huge strings), then again after cleaning."""
    truncated = False
    if len(text) > max_chars * 4:  # cheap pre-bound before the regex passes
        text, truncated = text[: max_chars * 4], True
    text = _CRLF.sub("\n", text)
    text = _SURROGATE.sub(_REPLACEMENT, text)
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)
    text = unicodedata.normalize("NFC", text)
    if len(text) > max_chars:
        text, truncated = text[:max_chars], True
    return text, truncated


def clean_label(value: str | None, *, max_chars: int = 64) -> str | None:
    """Single-line label (speaker name, file name): cleaned, no newlines or path
    separators, capped. None when nothing printable remains."""
    if value is None:
        return None
    s, _ = clean_text(value, max_chars=max_chars * 4)
    s = re.sub(r"[\n\t/\\]+", " ", s).strip()
    s = re.sub(r" {2,}", " ", s)[:max_chars].strip()
    return s or None


def decode_text_document(data: bytes) -> str | None:
    """Strict UTF-8 (BOM tolerated). None => not a text document: NUL bytes,
    invalid UTF-8, UTF-16 BOMs or a high control-character ratio (binary
    masquerading as .txt) are rejected."""
    if not data or b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return None
    sample = text[:4096]
    if sample:
        ctrl = sum(1 for c in sample if unicodedata.category(c) == "Cc" and c not in "\n\r\t")
        if ctrl / len(sample) > 0.02:
            return None
    return text


_MIME_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,62}/[a-z0-9][a-z0-9.+-]{0,62}")


def clean_mime(value: str | None) -> str | None:
    """A well-formed lower-case `type/subtype`, else None (never a raw string)."""
    if value is None or len(value) > 200:
        return None
    s, _ = clean_text(value, max_chars=200)
    s = s.strip().lower()
    return s if _MIME_RE.fullmatch(s) else None
