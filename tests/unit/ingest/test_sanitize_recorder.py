# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

from __future__ import annotations

from datetime import UTC, datetime

from src.ingest.recorder import parse_recorder_text, render_transcript
from src.ingest.sanitize import clean_label, clean_text, decode_text_document
from tests.unit.ingest.hostile import COMBINING_ACUTE, RLO

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def test_clean_text_removes_hidden_and_controls_but_keeps_layout() -> None:
    out, trunc = clean_text("a\u200bb\x00c\r\nd\te" + RLO + "\x1b[0m\ud800", max_chars=100)
    assert out == "abc\nd\te[0m" + chr(0xFFFD) and not trunc


def test_clean_text_truncates() -> None:
    out, trunc = clean_text("e" * 50, max_chars=10)
    assert len(out) == 10 and trunc


def test_clean_text_nfc() -> None:
    assert clean_text("e" + COMBINING_ACUTE, max_chars=10)[0] == "é"


def test_clean_label_strips_paths_and_newlines() -> None:
    assert clean_label("../a/b\\c\nd") == ".. a b c d"
    assert clean_label("\u200b\x00") is None
    assert clean_label(None) is None


def test_decode_rejects_binary_and_accepts_bom_utf8() -> None:
    assert decode_text_document(b"\xef\xbb\xbfhello") == "hello"
    assert decode_text_document(b"he\x00llo") is None
    assert decode_text_document(b"\xff\xfeh\x00") is None
    assert decode_text_document(b"\x01\x02\x03\x04" * 50) is None


def test_parse_headers_and_timestamps() -> None:
    t, trunc = parse_recorder_text(
        "Speaker 1  1:02:03\nhello\nthere\n\nSpeaker 2 0:05\nbye\n",
        max_segments=10,
        created_at=NOW,
    )
    assert not trunc and t.untrusted
    assert [(s.speaker_label, s.start_ms, s.text) for s in t.segments] == [
        ("Speaker 1", 3_723_000, "hello\nthere"),
        ("Speaker 2", 5_000, "bye"),
    ]
    assert render_transcript(t).startswith("Speaker 1: hello")


def test_parse_without_headers_splits_paragraphs() -> None:
    t, _ = parse_recorder_text("one\n\ntwo\n\n\nthree", max_segments=2, created_at=NOW)
    assert [s.text for s in t.segments] == ["one", "two"] and t.untrusted


def test_parse_pathological_lines_are_total() -> None:
    nasty = ("1:" * 20000) + "\n" + (" " * 50000) + "\n" + ("a:" * 30) + "9:99\n"
    t, _ = parse_recorder_text(nasty, max_segments=5, created_at=NOW)
    assert len(t.segments) <= 5
