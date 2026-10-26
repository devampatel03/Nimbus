# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Seams of the ingest layer.

`Transcriber` is the speech-to-text seam. `src/llm` has `Purpose.TRANSCRIBE`
but no audio-capable request/provider type (ChatRequest carries text only), so
there is no transcription provider to adapt yet. The Protocol is defined here;
an implementation that goes through the guarded `src.llm` path (scrub, audit,
kill switch, spend cap) is a follow-up. Ingest never imports a model SDK or a
transport.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from src.contracts.event import Event, EventKind


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    model: str
    language: str | None = None
    confidence: float | None = None


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, *, mime_type: str | None) -> TranscriptionResult: ...


class TranscriptionFailed(Exception):
    """Fixed-code failure (no audio, text or provider body in the message)."""

    def __init__(self, code: str = "transcription_failed") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PendingFollowOn:
    """A stored voice/recorder event with no transcript event yet."""

    id: uuid.UUID
    kind: EventKind
    lane: str
    meta: dict[str, Any]
    file_unique_id: str | None


class EventSink(Protocol):
    async def record(self, event: Event) -> tuple[uuid.UUID, bool]:
        """Idempotent on `event.source_key`. Returns `(event_id, created)`."""
        ...

    async def get_id(self, source_key: str) -> uuid.UUID | None: ...

    async def pending_follow_ons(self, limit: int) -> list[PendingFollowOn]: ...
