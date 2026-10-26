# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Event: the provenance root as a typed contract (TARGET_DESIGN section 2/4).

Mirrors the `event` table (src/memory/schemas/event.py). An Event is
immutable once stored (append-only trigger), so an asynchronous follow-on
(voice transcription) produces a *second* event whose `meta.parent_source_key`
names the first, rather than updating it.

INV-6: untrusted material (forwards, via_bot, Recorder transcripts, third-party
speech) carries `lane="untrusted"`. The contract itself refuses to build a
trusted Event of a kind that is untrusted by construction.
"""

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EventKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"  # the voice note itself (audio not retained)
    VOICE_TRANSCRIPT = "voice_transcript"
    RECORDER = "recorder"  # the .txt document as received
    RECORDER_TRANSCRIPT = "recorder_transcript"  # parsed, sanitised transcript text


MAX_META_BYTES = 8 * 1024

# Third-party speech by construction: never trusted, whoever forwarded it.
ALWAYS_UNTRUSTED = frozenset({EventKind.RECORDER, EventKind.RECORDER_TRANSCRIPT})


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str = Field(min_length=1, max_length=200)  # unique; idempotency key
    kind: EventKind
    source: str = Field(min_length=1, max_length=64)
    lane: Literal["trusted", "untrusted"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    body: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    file_unique_id: str | None = None
    occurred_at: datetime

    @model_validator(mode="after")
    def _meta_is_small(self) -> "Event":
        size = len(json.dumps(self.meta, default=str).encode("utf-8", "replace"))
        if size > MAX_META_BYTES:
            raise ValueError("event meta too large")
        return self

    @model_validator(mode="after")
    def _untrusted_kinds_stay_untrusted(self) -> "Event":
        if self.kind in ALWAYS_UNTRUSTED and self.lane != "untrusted":
            raise ValueError(f"{self.kind.value} events must be lane=untrusted")
        return self
