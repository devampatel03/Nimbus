# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Rendering: hostile-string escaping and chunking."""

from __future__ import annotations

import pytest

from src.gateway.render import (
    MAX_MESSAGE_CHARS,
    chunk_text,
    escape_html,
    escape_markdown_v2,
    render_chunks,
)

HOSTILE = [
    "<script>alert(1)</script>",
    '<a href="https://evil.example/x">click</a>',
    "<tg-spoiler>x</tg-spoiler> &amp; &lt; &#60;",
    "![img](https://evil.example/p.png?d=SECRET)",
    "[click](tg://user?id=1)",
    "\x00\x01nul\x1fbytes",
    "<<<<<<<<>>>>>>>>&&&&&&&&",
    "_*[]()~`>#+-=|{}.!\\",
]


@pytest.mark.parametrize("s", HOSTILE)
def test_html_escape_leaves_no_markup(s: str) -> None:
    out = escape_html(s)
    assert "<" not in out and ">" not in out
    # every ampersand starts one of our three entities
    import re

    assert not re.search(r"&(?!amp;|lt;|gt;)", out)


@pytest.mark.parametrize("s", HOSTILE)
def test_rendered_chunks_are_safe_and_control_free(s: str) -> None:
    for c in render_chunks(s):
        assert "<" not in c and ">" not in c
        assert "\x00" not in c and "\x1f" not in c


def test_markdown_v2_escapes_every_reserved_char() -> None:
    reserved = "_*[]()~`>#+-=|{}.!\\"
    out = escape_markdown_v2(reserved)
    for ch in reserved:
        assert rf"\{ch}" in out
    # no unescaped reserved char survives
    stripped = out.replace("\\\\", "")
    for i, ch in enumerate(stripped):
        if ch in "_*[]()~`>#+-=|{}.!":
            assert stripped[i - 1] == "\\"


def test_short_text_one_chunk() -> None:
    assert chunk_text("hello") == ["hello"]


def test_empty_and_whitespace() -> None:
    assert chunk_text("") == []
    assert chunk_text(" \n\n  ") == []


def test_chunks_cut_on_paragraph_boundaries() -> None:
    paras = [f"p{i} " + "x" * 1500 for i in range(6)]
    chunks = chunk_text("\n\n".join(paras))
    assert len(chunks) > 1
    assert all(len(escape_html(c)) <= MAX_MESSAGE_CHARS for c in chunks)
    for c in chunks:
        assert set(c.split("\n\n")) <= set(paras)  # no paragraph was cut
    assert "\n\n".join(chunks) == "\n\n".join(paras)


@pytest.mark.parametrize("ch", ["&", "<", "a", "é", "😀"])
def test_escaped_length_limit_with_expanding_chars(ch: str) -> None:
    text = ch * 20000
    out = render_chunks(text)
    assert all(len(c) <= MAX_MESSAGE_CHARS for c in out)
    assert "".join(out).replace("&amp;", "&").replace("&lt;", "<") == text


def test_long_single_paragraph_splits_on_lines_then_words() -> None:
    text = "\n".join(f"line {i} " + "w " * 40 for i in range(300))
    chunks = render_chunks(text)
    assert all(len(c) <= MAX_MESSAGE_CHARS for c in chunks)
    assert "\n".join(chunks).count("line ") == 300


def test_entity_never_split_across_chunks() -> None:
    chunks = render_chunks("&" * (MAX_MESSAGE_CHARS // 5 * 3))
    for c in chunks:
        assert c.count("&") == c.count(";")


@pytest.mark.parametrize("n", [2047, 2048, 2049, 4096])
def test_utf16_limit_with_astral_chars(n: int) -> None:
    out = render_chunks("😀" * n)
    for c in out:
        assert len(c.encode("utf-16-le")) // 2 <= MAX_MESSAGE_CHARS
    assert "".join(out) == "😀" * n
