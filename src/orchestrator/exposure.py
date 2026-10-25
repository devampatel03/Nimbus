# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Tool exposure selection (R16): at most 12 tools are shown to the model per
turn, however many are registered. Pure and deterministic.

Selection: tools flagged `always` first, then the rest ranked by keyword
overlap between the user message and the tool name/description, ties broken
by name. The model never chooses what it is shown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from src.llm.types import MAX_TOOLS, ToolSpec

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class ExposableTool:
    spec: ToolSpec
    always: bool = False


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def select_tools(
    candidates: Sequence[ExposableTool], user_text: str, *, limit: int = MAX_TOOLS
) -> tuple[ToolSpec, ...]:
    """Return at most `min(limit, 12)` specs."""
    cap = max(0, min(limit, MAX_TOOLS))
    query = _words(user_text)

    def rank(t: ExposableTool) -> tuple[int, int, str]:
        overlap = len(query & _words(f"{t.spec.name.replace('_', ' ')} {t.spec.description}"))
        return (0 if t.always else 1, -overlap, t.spec.name)

    seen: set[str] = set()
    out: list[ToolSpec] = []
    for t in sorted(candidates, key=rank):
        if len(out) >= cap:
            break
        if t.spec.name in seen:
            continue
        seen.add(t.spec.name)
        out.append(t.spec)
    return tuple(out)


def names(specs: Iterable[ToolSpec]) -> frozenset[str]:
    return frozenset(s.name for s in specs)
