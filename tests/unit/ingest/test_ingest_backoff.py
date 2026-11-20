# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Attempts are persisted BEFORE the paid call, spaced by backoff (a short
outage must not tombstone), claimed atomically, and notices do not flood."""

from __future__ import annotations

from datetime import timedelta

from src.ingest import Ingestor, IngestSettings, Outcome, UnavailableTranscriber
from src.ingest.protocols import TranscriptionFailed
from tests.unit.ingest.fakes import (
    CLK,
    FakeGateway,
    FakeSink,
    FakeTranscriber,
    voice_update,
)

DEFAULT = IngestSettings()  # production defaults: backoff 10 min / 1 h / 6 h


class Crash(BaseException):
    """Simulates the process dying mid paid call (not caught by `except Exception`)."""


def build(n: int, cfg: IngestSettings = DEFAULT, sink: FakeSink | None = None):
    calls: list[str] = []
    sink = sink or FakeSink(calls)
    calls = sink.calls
    gw = FakeGateway(calls, [voice_update(i) for i in range(1, n + 1)],
                     {f"fid{i}": b"OggS" for i in range(1, n + 1)})  # fmt: skip
    asr = FakeTranscriber(calls)
    return Ingestor(sink, gw, asr, cfg, clock=CLK), sink, gw, asr, calls


async def test_short_outage_does_not_tombstone_and_does_not_flood() -> None:
    ing, sink, gw, asr, calls = build(10)
    t_end = CLK.now + timedelta(minutes=15)

    orig = asr.transcribe

    async def flaky(audio: bytes, *, mime_type: str | None):  # type: ignore[no-untyped-def]
        if CLK.now < t_end:
            calls.append("transcribe")
            raise TranscriptionFailed("asr_down")
        return await orig(audio, mime_type=mime_type)

    asr.transcribe = flaky  # type: ignore[method-assign]
    await ing.run()  # attempt 1 for all ten, ASR down
    for _ in range(36):  # poll every 5 minutes for 3 hours
        CLK.advance(300)
        await ing.recover_pending(limit=20)
    stored = [k for k, (_, e) in sink.rows.items() if k.endswith(":transcript") and e.body]
    tombs = [k for k, (_, e) in sink.rows.items() if k.endswith(":transcript") and not e.body]
    assert len(stored) == 10 and not tombs
    assert len(gw.sent) <= 10  # at most one notice per note, none on later attempts
    assert not any("several tries" in m for m in gw.sent)


async def test_attempt_is_persisted_before_the_paid_call() -> None:
    ing, sink, _gw, asr, calls = build(1)

    async def dies(audio: bytes, *, mime_type: str | None):  # type: ignore[no-untyped-def]
        calls.append("transcribe")
        raise Crash

    asr.transcribe = dies  # type: ignore[method-assign]
    try:
        await ing.run()
    except Crash:
        pass
    eid = sink.rows["telegram:1"][0]
    assert sink.attempts[eid] == 1  # counted although the call never returned
    assert calls.index("bump:1") < calls.index("fetch:uniq1")


async def test_repeated_crashes_are_bounded_by_max_attempts() -> None:
    cfg = IngestSettings(max_attempts=3, backoff_s=(0.0,))
    ing, sink, _gw, asr, calls = build(1, cfg)

    async def dies(audio: bytes, *, mime_type: str | None):  # type: ignore[no-untyped-def]
        calls.append("transcribe")
        raise Crash

    asr.transcribe = dies  # type: ignore[method-assign]
    for _ in range(8):
        try:
            if not sink.rows:
                await ing.run()
            else:
                CLK.advance(601)
                await ing.recover_pending()
        except Crash:
            pass
    assert calls.count("transcribe") == 3
    assert sink.rows["telegram:1:transcript"][1].meta["rejected"] == "retries_exhausted"


async def test_failed_path_tombstones_exactly_at_max_attempts() -> None:
    """Kills the 'remove the attempt check in failed()' mutant."""
    cfg = IngestSettings(max_attempts=3, backoff_s=(0.0,))
    ing, sink, gw, asr, calls = build(1, cfg)
    asr.error = TranscriptionFailed("asr_down")
    await ing.run()
    for _ in range(10):
        CLK.advance(601)
        await ing.recover_pending()
    assert calls.count("transcribe") == 3
    assert sink.rows["telegram:1:transcript"][1].meta["rejected"] == "retries_exhausted"
    assert sum("several tries" in m for m in gw.sent) == 1  # one exhaustion notice


async def test_counter_exhausted_beyond_max_is_tombstoned_without_call() -> None:
    cfg = IngestSettings(max_attempts=3, backoff_s=(0.0,))
    ing, sink, _gw, _asr, calls = build(1, cfg)
    await ing.store(voice_update(1))
    sink.attempts[sink.rows["telegram:1"][0]] = 5
    sink.last_attempt[sink.rows["telegram:1"][0]] = CLK()
    CLK.advance(601)
    res = await ing.recover_pending()
    assert [(r.outcome, r.reason) for r in res] == [(Outcome.REJECTED, "retries_exhausted")]
    assert not any(c.startswith("fetch") for c in calls)


async def test_counter_cleared_when_stored_or_tombstoned() -> None:
    cfg = IngestSettings(max_attempts=2, backoff_s=(0.0,))
    ing, sink, _gw, asr, _calls = build(2, cfg)
    asr.error = TranscriptionFailed("x")
    await ing.run()
    asr.error = None
    CLK.advance(601)
    await ing.recover_pending()
    assert sink.attempts == {}  # one stored, one stored: counters deleted
    ing2, sink2, *_r = build(1, IngestSettings(max_attempts=1, backoff_s=(0.0,)))
    _r[1].error = TranscriptionFailed("x")
    await ing2.run()
    assert sink2.attempts == {}  # exhausted -> tombstoned -> cleared


async def test_two_instances_do_not_double_call() -> None:
    ing1, sink, gw, asr, calls = build(1)
    ing2 = Ingestor(sink, gw, asr, DEFAULT, clock=CLK)
    await ing1.store(voice_update(1))
    CLK.advance(601)
    asr.error = TranscriptionFailed("down")
    assert [r.outcome for r in await ing1.recover_pending()] == [Outcome.FAILED]
    assert await ing2.recover_pending() == []  # backoff + claim: not due for anyone
    assert calls.count("transcribe") == 1


async def test_asr_unavailable_has_its_own_notice() -> None:
    ing, _sink, gw, *_ = build(1)
    ing._asr = UnavailableTranscriber()
    await ing.run()
    assert len(gw.sent) == 1
    assert "transcri" in gw.sent[0].lower() and "unsupported" not in gw.sent[0].lower()
