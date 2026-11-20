# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Transcript: speaker-optional text of a voice note or Recorder export.

Replaces the retired DiarizedTranscript (phone-capture pipeline, gone in the
pivot). Speaker labels, timings and confidence are all optional because a
plain voice-note ASR result or a Recorder .txt export has none or only some.
`untrusted` is True for any third-party speech (INV-6); the extractor then
runs in the quarantine lane.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Segment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    speaker_label: str | None = None  # local label only, e.g. "Speaker 2"
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Transcript(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    segments: list[Segment]
    source_model: str
    language: str | None = None
    untrusted: bool
    created_at: datetime
