# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Outbound text rendering: escaping and chunking.

We send with `parse_mode=HTML` and escape *everything* (`&`, `<`, `>`), so
model/third-party text can never produce tags, links or mentions. Chunks are
cut from the raw text and escaped independently, so an entity is never split
across messages; the 4096 limit is applied to the *escaped* length (Telegram
counts characters after entity parsing, so this is conservative). A
`MarkdownV2` escaper is provided too, escaping the full reserved set from the
Bot API docs.

Exfil-oriented stripping (markdown images, URL allowlist) is P4.3's job and
extends this module.
"""

from __future__ import annotations

import re

MAX_MESSAGE_CHARS = 4096

_HTML_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_MDV2_RESERVED = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
# Telegram rejects NUL; other C0 controls (except \n, \t) render oddly.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text: str) -> str:
    return _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def escape_html(text: str) -> str:
    return "".join(_HTML_ESC.get(c, c) for c in text)


def escape_markdown_v2(text: str) -> str:
    return _MDV2_RESERVED.sub(r"\\\1", text)


def utf16_len(s: str) -> int:
    """Telegram counts message length in UTF-16 code units."""
    return len(s.encode("utf-16-le", errors="surrogatepass")) // 2


def _elen(s: str) -> int:
    return utf16_len(escape_html(s))


def _hard_split(piece: str, limit: int) -> list[str]:
    out: list[str] = []
    cur = ""
    cur_len = 0
    for ch in piece:
        n = _elen(ch)
        if cur_len + n > limit:
            out.append(cur)
            cur, cur_len = "", 0
        cur += ch
        cur_len += n
    if cur:
        out.append(cur)
    return out


def _split_oversize(piece: str, limit: int) -> list[str]:
    """Break a single over-limit paragraph on lines, then words, then chars."""
    for sep in ("\n", " "):
        if sep not in piece:
            continue
        out: list[str] = []
        cur = ""
        for part in piece.split(sep):
            cand = part if not cur else cur + sep + part
            if _elen(cand) <= limit:
                cur = cand
                continue
            if cur:
                out.append(cur)
            if _elen(part) <= limit:
                cur = part
            else:
                sub = _split_oversize(part, limit) if sep == "\n" else _hard_split(part, limit)
                out.extend(sub[:-1])
                cur = sub[-1]
        if cur:
            out.append(cur)
        return out
    return _hard_split(piece, limit)


def chunk_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split raw text into chunks whose *escaped* form is <= limit, preferring
    paragraph boundaries. Returns raw (unescaped) chunks; empty input -> []."""
    text = sanitize(text).strip()
    if not text:
        return []
    chunks: list[str] = []
    cur = ""
    for para in re.split(r"\n{2,}", text):
        if not para.strip():
            continue
        cand = para if not cur else cur + "\n\n" + para
        if _elen(cand) <= limit:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if _elen(para) <= limit:
            cur = para
        else:
            sub = _split_oversize(para, limit)
            chunks.extend(sub[:-1])
            cur = sub[-1]
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def render_chunks(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Ready-to-send HTML-escaped chunks."""
    return [escape_html(c) for c in chunk_text(text, limit)]
