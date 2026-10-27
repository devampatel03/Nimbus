# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Per-turn taint (ADR-0010 item 5, TARGET_DESIGN section 5).

A turn is tainted once any untrusted-derived data enters it: an untrusted
DataBlock, untrusted tool output, a tool error (may echo external text), or a
recalled untrusted memory item (even when its content is withheld). Taint is
set by code from code-declared flags (a tool's `ToolOutput.untrusted`, a memory
row's lane), never from model output. It is monotonic: there is no method that
clears it and its attributes cannot be assigned or deleted. The dispatcher sees
it as a boolean fact; P3.1's policy uses it (tainted WRITE_INTERNAL ->
require_approval) and owns the cross-turn window (turn + 3, ADR-0010).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, final

from src.lanes.data import DataBlock


class TaintSource(StrEnum):
    DATA_BLOCK = "data_block"
    UNTRUSTED_TOOL_OUTPUT = "untrusted_tool_output"
    TOOL_ERROR = "tool_error"
    UNTRUSTED_MEMORY = "untrusted_memory"
    QUARANTINE_OUTPUT = "quarantine_output"


@final
class Taint:
    __slots__ = ("_sources", "_tainted")

    _sources: set[TaintSource]
    _tainted: bool

    def __init__(self) -> None:
        object.__setattr__(self, "_tainted", False)
        object.__setattr__(self, "_sources", set())

    def __init_subclass__(cls, **kw: Any) -> None:
        raise TypeError("Taint cannot be subclassed")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("taint can only be marked, never assigned")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("taint can only be marked, never cleared")

    def mark(self, source: TaintSource) -> None:
        if not isinstance(source, TaintSource):
            raise TypeError("taint source must be a TaintSource")
        self._sources.add(source)
        object.__setattr__(self, "_tainted", True)

    def absorb(self, block: DataBlock) -> None:
        """Called for every DataBlock that enters the turn."""
        if not isinstance(block, DataBlock):
            raise TypeError("absorb takes a DataBlock")
        if block.untrusted:
            self.mark(TaintSource.DATA_BLOCK)

    @property
    def tainted(self) -> bool:
        return self._tainted

    @property
    def sources(self) -> frozenset[TaintSource]:
        return frozenset(self._sources)

    def __repr__(self) -> str:
        names = ",".join(sorted(s.value for s in self._sources))
        return f"Taint(tainted={self._tainted}, sources=[{names}])"


__all__ = ["Taint", "TaintSource"]
