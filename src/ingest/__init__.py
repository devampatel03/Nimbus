# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

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
