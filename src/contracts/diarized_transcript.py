# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""DiarizedTranscript: Perception agent -> Memory agent. Frozen at P2.

Output of the Mode B batch pipeline (section 5.2): ASR plus diarization,
speaker-attributed where a voice-print match succeeded.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Utterance(BaseModel):
    model_config = ConfigDict(frozen=True)

    speaker_id: UUID | None = None  # None until enrolled/matched
    speaker_label: str  # local cluster label, e.g. "speaker_2"
    text: str
    start_ms: int
    end_ms: int
    confidence: float
    language: str


class DiarizedTranscript(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    utterances: list[Utterance]
    source_asr_model: str
    created_at: datetime
