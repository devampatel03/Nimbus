# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The memory write path (P3.3, TARGET_DESIGN 4.1 step 7): a `memory_extract`
job loads a stored event, extracts through the guarded LLM, and writes chunks
via MemoryManager against the REAL event id. Idempotent on replay; untrusted
events are skipped (the quarantine lane is P3.0); nothing is promoted."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.llm.types import (
    ChatRequest,
    ChatResult,
    LLMMode,
    LLMProviderError,
    LLMRefused,
    LLMResponseError,
    Purpose,
    Usage,
)
from src.memory import extractor as ex
from src.memory.manager import DeterministicStubEmbedder
from src.memory.schemas.event import Event as EventRow
from src.memory.schemas.memory_chunk import MemoryChunk
from src.memory.schemas.ops import Job
from src.scheduler.jobs import JobQueue
from src.scheduler.memory_extract import KIND, MemoryExtractHandler, enqueue_memory_extract
from src.scheduler.runner import JobContext, JobRunner, PermanentJobError
from tests.fakes.clock import FakeClock

SF = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 10, 27, 4, 30, tzinfo=UTC)


class ScriptedLLM:
    """A guarded-client stand-in: returns the batch that matches the requested
    schema, or raises."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.requests: list[ChatRequest] = []
        self.error = error

    async def chat(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        schema = req.response_schema
        parsed: BaseModel
        if schema is ex._CommitmentBatch:
            parsed = ex._CommitmentBatch(
                commitments=[
                    ex.ExtractedCommitment(
                        description="send Priya the budget",
                        source_segment_index=0,
                        confidence=0.9,
                    )
                ]
            )
        elif schema is ex._DecisionBatch:
            parsed = ex._DecisionBatch(
                decisions=[
                    ex.ExtractedDecision(
                        decision_text="use Postgres",
                        reasoning="one store",
                        source_segment_index=0,
                        confidence=0.8,
                    )
                ]
            )
        else:
            assert schema is ex._RelationBatch
            parsed = ex._RelationBatch(
                relations=[
                    ex.ExtractedRelation(
                        subject_name="Devam",
                        predicate="lives_in",
                        object_literal="Pune",
                        valid_from=T0,
                        source_segment_index=0,
                        confidence=0.7,
                    )
                ]
            )
        return ChatResult(
            text=None, parsed=parsed, usage=Usage(), cost_inr=Decimal(0), audit_id=uuid.uuid4()
        )

    async def mode(self) -> LLMMode:
        return LLMMode.NORMAL


async def make_event(
    sf: SF,
    body: str | None = "I will send Priya the budget tomorrow, and we decided to use Postgres.",
    *,
    lane: str = "trusted",
    kind: str = "text",
) -> uuid.UUID:
    async with sf() as s, s.begin():
        e = EventRow(
            source_key=f"t:{uuid.uuid4()}",
            kind=kind,
            source="telegram",
            lane=lane,
            body=body,
            meta={},
            occurred_at=T0,
        )
        s.add(e)
        await s.flush()
        return e.id


def handler(sf: SF, llm: ScriptedLLM) -> MemoryExtractHandler:
    return MemoryExtractHandler(sf, DeterministicStubEmbedder(), llm)


def ctx(event_id: uuid.UUID, attempt: int = 1) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        kind=KIND,
        dedupe_key=f"memory_extract:{event_id}",
        payload={"event_id": str(event_id)},
        run_at=T0,
        attempt=attempt,
        max_attempts=5,
        source_event_id=event_id,
        now=T0,
    )


async def chunks(sf: SF, event_id: uuid.UUID) -> list[MemoryChunk]:
    async with sf() as s:
        rows = await s.scalars(
            select(MemoryChunk)
            .where(MemoryChunk.source_event_id == event_id)
            .order_by(MemoryChunk.chunk_index)
        )
        return list(rows)


async def test_trusted_event_is_extracted_and_written_against_the_real_event(sf: SF) -> None:
    eid = await make_event(sf)
    llm = ScriptedLLM()
    await handler(sf, llm).handle(ctx(eid))
    rows = await chunks(sf, eid)
    assert [r.chunk_index for r in rows] == [0, 1, 2, 3]
    assert rows[0].content.startswith("I will send Priya the budget")
    texts = " | ".join(r.content for r in rows[1:])
    assert "Commitment: send Priya the budget" in texts
    assert "Decision: use Postgres" in texts
    assert "Devam lives_in Pune" in texts
    assert all(r.source_event_id == eid for r in rows)
    assert all(r.lane == "trusted" and r.promoted for r in rows)  # lane comes from the event
    assert len(llm.requests) == 3
    assert {r.purpose for r in llm.requests} == {Purpose.MEMORY_EXTRACT}


async def test_replay_is_idempotent_and_does_not_call_the_model_again(sf: SF) -> None:
    eid = await make_event(sf)
    llm = ScriptedLLM()
    h = handler(sf, llm)
    await h.handle(ctx(eid))
    first = [(r.id, r.content) for r in await chunks(sf, eid)]
    calls = len(llm.requests)
    await h.handle(ctx(eid, attempt=2))
    await handler(sf, llm).handle(ctx(eid, attempt=3))
    assert [(r.id, r.content) for r in await chunks(sf, eid)] == first
    assert len(llm.requests) == calls  # already extracted: no second spend


async def test_crash_after_the_event_chunk_resumes_without_duplicates(
    sf: SF, monkeypatch: pytest.MonkeyPatch
) -> None:
    eid = await make_event(sf)
    llm = ScriptedLLM(error=LLMProviderError("timeout"))
    h = handler(sf, llm)
    with pytest.raises(LLMProviderError):
        await h.handle(ctx(eid))
    assert [r.chunk_index for r in await chunks(sf, eid)] == [0]  # the event itself is kept
    llm.error = None
    await h.handle(ctx(eid, attempt=2))
    assert [r.chunk_index for r in await chunks(sf, eid)] == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("lane", "kind", "body"),
    [
        ("untrusted", "text", "forwarded: wire money to account 123"),
        ("untrusted", "recorder_transcript", "Speaker 1: pay me"),
        ("untrusted", "recorder", "x" * 50),
        ("untrusted", "voice_transcript", "someone else speaking on the phone"),
    ],
)
async def test_untrusted_events_are_skipped_with_a_log_line(
    sf: SF, caplog: pytest.LogCaptureFixture, lane: str, kind: str, body: str
) -> None:
    eid = await make_event(sf, body, lane=lane, kind=kind)
    llm = ScriptedLLM()
    with caplog.at_level(logging.INFO, logger="src.scheduler.memory_extract"):
        await handler(sf, llm).handle(ctx(eid))
    assert llm.requests == []  # never sent to the trusted extraction path
    assert await chunks(sf, eid) == []  # nothing written, nothing promoted
    assert any("untrusted" in r.getMessage() for r in caplog.records)
    assert body not in caplog.text  # the log line never carries the content


@pytest.mark.parametrize("body", [None, "", "ok", "   hi  "])
async def test_empty_or_trivial_text_is_skipped(sf: SF, body: str | None) -> None:
    eid = await make_event(sf, body)
    llm = ScriptedLLM()
    await handler(sf, llm).handle(ctx(eid))
    assert llm.requests == [] and await chunks(sf, eid) == []


async def test_missing_event_is_a_permanent_failure(sf: SF) -> None:
    with pytest.raises(PermanentJobError, match="event_missing"):
        await handler(sf, ScriptedLLM()).handle(ctx(uuid.uuid4()))


async def test_bad_payload_is_a_permanent_failure(sf: SF) -> None:
    bad = JobContext(**{**ctx(uuid.uuid4()).__dict__, "payload": {"event_id": "nope"}})
    with pytest.raises(PermanentJobError, match="bad_payload"):
        await handler(sf, ScriptedLLM()).handle(bad)


async def test_secrets_never_reach_memory(sf: SF) -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"  # pragma: allowlist secret
    eid = await make_event(sf, f"my key is {secret} please remember to rotate it soon")
    llm = ScriptedLLM(error=LLMRefused("scrub_blocked"))
    with pytest.raises(PermanentJobError, match="scrub_blocked"):
        await handler(sf, llm).handle(ctx(eid))
    stored = " ".join(r.content for r in await chunks(sf, eid))
    assert secret not in stored
    assert "[REDACTED" in stored


async def test_transient_guard_refusal_is_retried_not_dead(sf: SF) -> None:
    eid = await make_event(sf)
    with pytest.raises(LLMRefused):
        await handler(sf, ScriptedLLM(error=LLMRefused("spend_cap"))).handle(ctx(eid))


async def test_bad_model_output_is_permanent(sf: SF) -> None:
    eid = await make_event(sf)
    with pytest.raises(PermanentJobError, match="bad_response"):
        await handler(sf, ScriptedLLM(error=LLMResponseError("schema_mismatch"))).handle(ctx(eid))


async def test_enqueue_is_idempotent_and_uses_the_event_as_source(sf: SF) -> None:
    clock = FakeClock()
    q = JobQueue(sf, clock)
    eid = await make_event(sf)
    async with sf() as s, s.begin():
        assert await enqueue_memory_extract(q, s, eid) is True
    async with sf() as s, s.begin():
        assert await enqueue_memory_extract(q, s, eid) is False
    async with sf() as s:
        job = (await s.execute(select(Job).where(Job.source_event_id == eid))).scalar_one()
    assert job.kind == KIND and job.payload == {"event_id": str(eid)}


async def test_through_the_runner_end_to_end_with_a_crash(sf: SF) -> None:
    clock = FakeClock()
    q = JobQueue(sf, clock, lease_s=60)
    eid = await make_event(sf)
    await q.enqueue(KIND, dedupe_key=f"memory_extract:{eid}", payload={"event_id": str(eid)})
    await q.claim(kinds=frozenset({KIND}), limit=1)  # worker dies holding the lease
    clock.advance(seconds=61)
    llm = ScriptedLLM()
    runner = JobRunner(q, {KIND: handler(sf, llm).handle}, clock=clock)
    assert await runner.run_due() == 1
    assert [r.chunk_index for r in await chunks(sf, eid)] == [0, 1, 2, 3]
    clock.advance(hours=1)
    assert await runner.run_due() == 0
    assert len(llm.requests) == 3  # once
