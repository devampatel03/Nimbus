# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Pixel Recorder-style `.txt` export -> `Transcript`.

Tolerant, bounded parser. Recognised header line: a short speaker label
optionally followed by a timestamp (`Speaker 1  0:03`, `Speaker 2 01:02:03`,
or a bare timestamp). Lines until the next header form that segment's text.
Without any header the text is split into paragraphs with no speaker. The
parse is a pure function of already-cleaned text, executes nothing, and the
result is always `untrusted` (third-party speech).
"""

from __future__ import annotations

import re
from datetime import datetime

from src.contracts.transcript import Segment, Transcript
from src.ingest.sanitize import clean_label, clean_text

_HEADER = re.compile(
    r"^(?P<who>[A-Za-z0-9][A-Za-z0-9 ._'-]{0,39}?)?\s*\(?(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\)?$"
)
_MAX_SEGMENT_CHARS = 8000


def _ms(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    sec = (
        parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
    )
    return sec * 1000


def parse_recorder_text(
    text: str, *, max_segments: int, created_at: datetime
) -> tuple[Transcript, bool]:
    """Returns `(transcript, truncated)`. `text` must already be `clean_text`ed."""
    segments: list[Segment] = []
    truncated = False
    who: str | None = None
    start: int | None = None
    buf: list[str] = []
    saw_header = False

    def flush() -> None:
        nonlocal truncated
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        if len(segments) >= max_segments:
            truncated = True
            return
        segments.append(Segment(text=body[:_MAX_SEGMENT_CHARS], speaker_label=who, start_ms=start))
        if len(body) > _MAX_SEGMENT_CHARS:
            truncated = True

    for line in text.split("\n"):
        stripped = line.strip()
        m = _HEADER.match(stripped) if stripped and len(stripped) <= 60 else None
        if m:
            flush()
            saw_header = True
            who = clean_label(m.group("who")) or who
            start = _ms(m.group("ts"))
        elif stripped or buf:
            buf.append(stripped)
    flush()
    if not saw_header:
        segments = []
        for para in re.split(r"\n\s*\n", text):
            body, _ = clean_text(para.strip(), max_chars=_MAX_SEGMENT_CHARS)
            if not body:
                continue
            if len(segments) >= max_segments:
                truncated = True
                break
            segments.append(Segment(text=body))
    return (
        Transcript(
            segments=segments,
            source_model="recorder-export",
            untrusted=True,
            created_at=created_at,
        ),
        truncated,
    )


def render_transcript(t: Transcript) -> str:
    return "\n".join(
        f"{s.speaker_label}: {s.text}" if s.speaker_label else s.text for s in t.segments
    )
