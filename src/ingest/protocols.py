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
from datetime import datetime
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


class EventStoreError(Exception):
    """Fixed-code storage failure. Carries no SQL, parameters or user text (INV-7)."""

    def __init__(self, code: str = "event_store_failed") -> None:
        self.code = code
        super().__init__(code)


class TranscriptionUnavailable(Exception):
    """No transcription provider is configured: permanent, not retryable."""

    code = "asr_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


class UnavailableTranscriber:
    """Default until a guarded `src.llm` audio provider exists."""

    async def transcribe(self, audio: bytes, *, mime_type: str | None) -> TranscriptionResult:
        raise TranscriptionUnavailable


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
    occurred_at: datetime
    attempts: int = 0


class EventSink(Protocol):
    async def record(self, event: Event) -> tuple[uuid.UUID, bool]:
        """Idempotent on `event.source_key`. Returns `(event_id, created)`."""
        ...

    async def get_id(self, source_key: str) -> uuid.UUID | None: ...

    async def bump_attempts(
        self, event_id: uuid.UUID, *, now: datetime, backoff_s: tuple[float, ...]
    ) -> int | None:
        """Atomically claim the next follow-on attempt for a parent: persist and
        return the new count, or None if the next attempt is not yet due (backoff
        not elapsed, or another instance claimed it). Called BEFORE the paid call."""
        ...

    async def clear_attempts(self, event_id: uuid.UUID) -> None:
        """Drop the counter once the follow-on is stored or tombstoned."""
        ...

    async def pending_follow_ons(
        self, limit: int, *, older_than: datetime, now: datetime, backoff_s: tuple[float, ...]
    ) -> list[PendingFollowOn]:
        """Recoverable parents (file_id present, no transcript/tombstone event,
        `occurred_at <= older_than`, backoff elapsed), fewest attempts first, oldest first."""
        ...
