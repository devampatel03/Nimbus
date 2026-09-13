# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Extraction worker interfaces (entity, relation, commitment, decision).

Per PSU-5 (docs/TECHNICAL_ARCHITECTURE.md section 7.1) extraction runs on a
schedule, never inline in a Mode A turn — these workers are consumed by
`src/workers/`, not the request path. Actual model calls are stubbed here:
no pod has wired a local or scrubbed-cloud model call yet, and doing so is
out of scope for this task. `_call_model` raises `NotImplementedError` so a
caller cannot silently get empty results and mistake it for "nothing found."

INV-1 note: `_call_model` must never itself perform network egress. When a
real implementation lands, any cloud call it needs MUST go through
`src/security/scrubber.py:execute_external`, not a direct HTTP call here.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.contracts.diarized_transcript import DiarizedTranscript


class ExtractedEntity(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_type: str
    canonical_name: str
    source_utterance_index: int
    confidence: float


class ExtractedRelation(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject_name: str
    predicate: str
    object_name: str | None = None
    object_literal: str | None = None
    valid_from: datetime
    source_utterance_index: int
    confidence: float


class ExtractedCommitment(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str
    owner_label: str | None = None  # speaker_label, resolved to person_id later
    due_at: datetime | None = None
    source_utterance_index: int
    confidence: float


class ExtractedDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_text: str
    reasoning: str
    source_utterance_index: int
    confidence: float


T = TypeVar("T", bound=BaseModel)


class ExtractionWorker(ABC, Generic[T]):
    """Base class for one extraction pass over a `DiarizedTranscript`.

    `T` is the extracted-item model this worker produces (one of the
    `Extracted*` models above). Concrete workers implement `extract`;
    resolving extracted names/labels to `graph_node`/`person` ids is the
    caller's job (`MemoryManager.write_semantic`), not this worker's.
    """

    @abstractmethod
    async def extract(self, transcript: DiarizedTranscript, *, source_session: UUID) -> list[T]: ...

    async def _call_model(self, prompt: str) -> str:
        raise NotImplementedError(
            "stub: no extraction model is wired yet — this method exists to "
            "fix the call boundary before a real (local or scrubbed-cloud) "
            "model is integrated"
        )


class EntityExtractor(ExtractionWorker[ExtractedEntity]):
    async def extract(
        self, transcript: DiarizedTranscript, *, source_session: UUID
    ) -> list[ExtractedEntity]:
        raise NotImplementedError("stub: see ExtractionWorker._call_model")


class RelationExtractor(ExtractionWorker[ExtractedRelation]):
    async def extract(
        self, transcript: DiarizedTranscript, *, source_session: UUID
    ) -> list[ExtractedRelation]:
        raise NotImplementedError("stub: see ExtractionWorker._call_model")


class CommitmentExtractor(ExtractionWorker[ExtractedCommitment]):
    async def extract(
        self, transcript: DiarizedTranscript, *, source_session: UUID
    ) -> list[ExtractedCommitment]:
        raise NotImplementedError("stub: see ExtractionWorker._call_model")


class DecisionExtractor(ExtractionWorker[ExtractedDecision]):
    async def extract(
        self, transcript: DiarizedTranscript, *, source_session: UUID
    ) -> list[ExtractedDecision]:
        raise NotImplementedError("stub: see ExtractionWorker._call_model")
