# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Titles for reminders and tasks: model- or user-supplied text, stored and
shown back, so control characters are dropped and length is bounded (the DB
CHECK is the backstop)."""

from __future__ import annotations

import unicodedata

MAX_TITLE_CHARS = 500
_WHITESPACE_CONTROLS = "\r\n\t"


class TitleError(ValueError):
    pass


def clean_title(raw: str) -> str:
    kept: list[str] = []
    for ch in raw:
        if ch in _WHITESPACE_CONTROLS:
            kept.append(" ")
        elif unicodedata.category(ch)[0] != "C":
            kept.append(ch)
    cleaned = " ".join("".join(kept).split())
    if not cleaned:
        raise TitleError("A title is required.")
    if len(cleaned) > MAX_TITLE_CHARS:
        raise TitleError(f"The title is too long (limit {MAX_TITLE_CHARS} characters).")
    return cleaned
