# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Ingest layer (P2.2): gateway updates -> `Event` rows."""

from src.ingest.pipeline import (
    FollowOnResult,
    Ingestor,
    IngestSettings,
    Outcome,
    StoreResult,
    transcript_from_event,
)
from src.ingest.protocols import (
    EventSink,
    PendingFollowOn,
    Transcriber,
    TranscriptionFailed,
    TranscriptionResult,
    UnavailableTranscriber,
)
from src.ingest.repository import PgEventSink

__all__ = [
    "EventSink",
    "FollowOnResult",
    "IngestSettings",
    "Ingestor",
    "Outcome",
    "PendingFollowOn",
    "PgEventSink",
    "StoreResult",
    "Transcriber",
    "TranscriptionFailed",
    "TranscriptionResult",
    "UnavailableTranscriber",
    "transcript_from_event",
]
