# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Extraction worker implementations (entity, relation, commitment,
decision) -- this session, resolves the earlier stub.

Per PSU-5 (docs/TECHNICAL_ARCHITECTURE.md section 7.1) extraction runs on a
schedule, never inline in a Mode A turn -- these workers are consumed by
`src/workers/`, not the request path, so a local-model call here has no
Mode A latency implication (unlike `think`/`respond` in
`src/orchestrator/graph.py`, which do).

Each worker calls the local LLM (`src/llm/client.py`, NOT cloud egress --
INV-1 unaffected, same as the rest of this session) via `ExtractionWorker`'s
`_call_model`, using structured output so the model's response is parsed
straight into the relevant `Extracted*` Pydantic model rather than a raw
string a caller must parse itself. `llm_client` is injectable (defaults to
`VLLMClient()`, same default-factory pattern as `Embedder`/`LLMClient`
elsewhere), so tests can supply a fake without hitting a real server.

INV-1 note (unchanged from the stub): `_call_model` must never itself
perform cloud egress. If a future extractor ever needs a cloud model
instead of the local one, that call MUST go through
`src/security/scrubber.py:execute_external`, not a direct call here.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.diarized_transcript import DiarizedTranscript
from src.llm.client import ChatMessage, LLMClient, VLLMClient


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
_SchemaT = TypeVar("_SchemaT", bound=BaseModel)


def _transcript_text(transcript: DiarizedTranscript) -> str:
    return "\n".join(
        f"[{i}] {u.speaker_label}: {u.text}" for i, u in enumerate(transcript.utterances)
    )


class ExtractionWorker(ABC, Generic[T]):
    """Base class for one extraction pass over a `DiarizedTranscript`.

    `T` is the extracted-item model this worker produces (one of the
    `Extracted*` models above). Concrete workers implement `extract`;
    resolving extracted names/labels to `graph_node`/`person` ids is the
    caller's job (`MemoryManager.write_semantic`), not this worker's.
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self._llm_client = llm_client or VLLMClient()

    @abstractmethod
    async def extract(self, transcript: DiarizedTranscript, *, source_session: UUID) -> list[T]: ...

    async def _call_model(self, messages: list[ChatMessage], *, schema: type[_SchemaT]) -> _SchemaT:
        """Structured-output call boundary. See module docstring's INV-1
        note -- this only ever reaches `self._llm_client` (local), never a
        transport library directly.
        """
        return await self._llm_client.structured(messages, schema=schema)


_ENTITY_SYSTEM_PROMPT = (
    "Extract named entities (people, organizations, places, products) "
    "mentioned in this diarized conversation transcript. For each entity "
    "give: entity_type, canonical_name, the source_utterance_index (the "
    "bracketed number in the transcript) where it is first clearly "
    "mentioned, and your confidence (0.0-1.0). Respond only via the given "
    "JSON schema; return an empty list if none are found."
)


class _EntityBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: list[ExtractedEntity] = Field(default_factory=list)


class EntityExtractor(ExtractionWorker[ExtractedEntity]):
    async def extract(
        self, transcript: DiarizedTranscript, *, source_session: UUID
    ) -> list[ExtractedEntity]:
        del source_session  # resolving names to graph_node ids is the caller's job
        messages: list[ChatMessage] = [
            {"role": "system", "content": _ENTITY_SYSTEM_PROMPT},
            {"role": "user", "content": _transcript_text(transcript)},
        ]
        batch = await self._call_model(messages, schema=_EntityBatch)
        return list(batch.entities)


_RELATION_SYSTEM_PROMPT = (
    "Extract factual relations (subject-predicate-object statements) from "
    "this diarized conversation transcript. For each relation give: "
    "subject_name, predicate, object_name (another entity) or "
    "object_literal (a plain value) -- exactly one of the two, "
    "valid_from (ISO 8601 datetime; use {created_at} if no more specific "
    "time is stated), source_utterance_index, and confidence (0.0-1.0). "
    "Respond only via the given JSON schema; return an empty list if none "
    "are found."
)


class _RelationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relations: list[ExtractedRelation] = Field(default_factory=list)


class RelationExtractor(ExtractionWorker[ExtractedRelation]):
    async def extract(
        self, transcript: DiarizedTranscript, *, source_session: UUID
    ) -> list[ExtractedRelation]:
        del source_session
        messages: list[ChatMessage] = [
            {
                "role": "system",
                "content": _RELATION_SYSTEM_PROMPT.format(
                    created_at=transcript.created_at.isoformat()
                ),
            },
            {"role": "user", "content": _transcript_text(transcript)},
        ]
        batch = await self._call_model(messages, schema=_RelationBatch)
        return list(batch.relations)


_COMMITMENT_SYSTEM_PROMPT = (
    "Extract commitments (things a speaker said they will do) from this "
    "diarized conversation transcript. For each commitment give: "
    "description, owner_label (the speaker_label who made the commitment, "
    "or null if unclear), due_at (ISO 8601 datetime, or null if unstated), "
    "source_utterance_index, and confidence (0.0-1.0). Respond only via "
    "the given JSON schema; return an empty list if none are found."
)


class _CommitmentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commitments: list[ExtractedCommitment] = Field(default_factory=list)


class CommitmentExtractor(ExtractionWorker[ExtractedCommitment]):
    async def extract(
        self, transcript: DiarizedTranscript, *, source_session: UUID
    ) -> list[ExtractedCommitment]:
        del source_session
        messages: list[ChatMessage] = [
            {"role": "system", "content": _COMMITMENT_SYSTEM_PROMPT},
            {"role": "user", "content": _transcript_text(transcript)},
        ]
        batch = await self._call_model(messages, schema=_CommitmentBatch)
        return list(batch.commitments)


_DECISION_SYSTEM_PROMPT = (
    "Extract decisions (conclusions the speakers reached) from this "
    "diarized conversation transcript. For each decision give: "
    "decision_text, reasoning (why, in the speakers' own terms), "
    "source_utterance_index, and confidence (0.0-1.0). Respond only via "
    "the given JSON schema; return an empty list if none are found."
)


class _DecisionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[ExtractedDecision] = Field(default_factory=list)


class DecisionExtractor(ExtractionWorker[ExtractedDecision]):
    async def extract(
        self, transcript: DiarizedTranscript, *, source_session: UUID
    ) -> list[ExtractedDecision]:
        del source_session
        messages: list[ChatMessage] = [
            {"role": "system", "content": _DECISION_SYSTEM_PROMPT},
            {"role": "user", "content": _transcript_text(transcript)},
        ]
        batch = await self._call_model(messages, schema=_DecisionBatch)
        return list(batch.decisions)
