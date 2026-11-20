# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Recovery is bounded (attempt budget, minimum age, no starvation) and honest."""

from __future__ import annotations

from src.contracts.event import EventKind
from src.ingest import Ingestor, IngestSettings, Outcome, UnavailableTranscriber
from src.ingest.protocols import TranscriptionFailed
from tests.unit.ingest.fakes import (
    CLK,
    FakeGateway,
    FakeSink,
    FakeTranscriber,
    voice_update,
)

CFG = IngestSettings(
    max_attempts=3, min_recover_age_s=600, max_recover_age_s=86_400, backoff_s=(0.0,)
)
MIN = 601


def build(n_updates: int, files: dict[str, bytes] | None = None):
    calls: list[str] = []
    sink = FakeSink(calls)
    ups = [voice_update(i) for i in range(1, n_updates + 1)]
    gw = FakeGateway(calls, ups, files or {f"fid{i}": b"OggS" for i in range(1, n_updates + 1)})
    asr = FakeTranscriber(calls)
    return Ingestor(sink, gw, asr, CFG, clock=CLK), sink, gw, asr, calls


def asr_calls(calls: list[str]) -> int:
    return calls.count("transcribe")


async def test_always_failing_asr_is_bounded_per_event() -> None:
    ing, sink, _gw, asr, calls = build(20)
    asr.error = TranscriptionFailed("asr_down")
    await ing.run()  # attempt 1 for each
    for _ in range(5):
        CLK.advance(MIN)
        await ing.recover_pending(limit=20)
    assert asr_calls(calls) <= 20 * CFG.max_attempts
    for i in range(1, 21):
        tomb = sink.rows[f"telegram:{i}:transcript"][1]
        assert tomb.body is None and tomb.meta["rejected"] == "retries_exhausted"


async def test_healthy_later_event_is_not_starved_by_failing_ones() -> None:
    ing, sink, gw, asr, _calls = build(5)
    asr.error = TranscriptionFailed("asr_down")
    await ing.run()  # 5 events, attempts == 1 each, all still pending
    asr.error = None
    # a healthy event whose follow-on never ran (crash after ack), stored later
    await ing.store(voice_update(99))
    gw.files["fid99"] = b"OggS"
    CLK.advance(MIN)
    res = await ing.recover_pending(limit=1)  # window of ONE slot
    assert [r.outcome for r in res] == [Outcome.STORED]
    assert "telegram:99:transcript" in sink.rows


async def test_minimum_age_avoids_racing_run() -> None:
    ing, _sink, _gw, _asr, calls = build(1)
    await ing.store(voice_update(1))  # stored, follow-on in flight elsewhere
    CLK.advance(30)
    assert await ing.recover_pending() == []
    assert not any(c.startswith("fetch") for c in calls)
    CLK.advance(MIN)
    assert [r.outcome for r in await ing.recover_pending()] == [Outcome.STORED]


async def test_parent_older_than_max_age_is_tombstoned_without_download() -> None:
    ing, _sink, _gw, _asr, calls = build(1)
    await ing.store(voice_update(1))
    CLK.advance(CFG.max_recover_age_s + 1)
    res = await ing.recover_pending()
    assert [(r.outcome, r.reason) for r in res] == [(Outcome.REJECTED, "retries_exhausted")]
    assert not any(c.startswith("fetch") for c in calls)


async def test_rows_without_file_id_are_excluded_and_hold_no_slot() -> None:
    ing, sink, gw, _asr, _calls = build(0)
    from src.contracts.event import Event

    for i in range(1, 6):  # unrecoverable: voice events lacking meta.file_id
        await sink.record(
            Event(
                source_key=f"telegram:{i}",
                kind=EventKind.VOICE,
                source="telegram",
                lane="trusted",
                meta={"update_id": i},
                file_unique_id=f"u{i}",
                occurred_at=CLK(),
            )
        )
    await ing.store(voice_update(50))
    gw.files["fid50"] = b"OggS"
    CLK.advance(MIN)
    res = await ing.recover_pending(limit=1)
    assert [r.outcome for r in res] == [Outcome.STORED]


async def test_concurrent_run_inflight_is_skipped_by_recover() -> None:
    ing, _sink, _gw, asr, calls = build(1)
    started = []

    orig = asr.transcribe

    async def slow(audio: bytes, *, mime_type: str | None):  # type: ignore[no-untyped-def]
        started.append(1)
        CLK.advance(MIN)  # time passes mid-follow-on
        assert await ing.recover_pending() == []  # in flight: must not duplicate
        return await orig(audio, mime_type=mime_type)

    asr.transcribe = slow  # type: ignore[method-assign]
    await ing.run()
    assert started == [1] and asr_calls(calls) == 1


async def test_failure_notice_is_honest_about_retry() -> None:
    ing, _sink, gw, asr, _calls = build(1)
    asr.error = TranscriptionFailed("asr_down")
    await ing.run()
    assert "retry" in gw.sent[0].lower() and "only if" in gw.sent[0].lower()
    for _ in range(4):
        CLK.advance(MIN)
        await ing.recover_pending()
    assert "resend" in gw.sent[-1].lower() and "retry" not in gw.sent[-1].lower()


async def test_unavailable_transcriber_tombstones_permanently() -> None:
    calls: list[str] = []
    sink = FakeSink(calls)
    gw = FakeGateway(calls, [voice_update(1)], {"fid1": b"OggS"})
    ing = Ingestor(sink, gw, UnavailableTranscriber(), CFG, clock=CLK)
    await ing.run()
    assert sink.rows["telegram:1:transcript"][1].meta["rejected"] == "asr_unavailable"
    CLK.advance(MIN)
    assert await ing.recover_pending() == []
