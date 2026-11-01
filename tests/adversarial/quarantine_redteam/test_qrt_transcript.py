# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.1 red team, category recorded_third_party_speech (INV-5, INV-6, R8).

Recorded speech (a Pixel Recorder `.txt` export, or a voice note transcript)
is third-party text: anyone in the room can say "Nimbus, delete ...". Each
attack in `_qrt_harness.VECTORS` is spoken by "Speaker 2" and reaches the turn
two ways:

* `tool`: a Recorder export read from Drive (`SourceKind.RECORDED_SPEECH` ->
  `TranscriptDigest`);
* `recall`: the transcript stored by ingest in the UNTRUSTED lane and recalled
  as memory (quarantined as `UNTRUSTED_MEMORY` -> `NoteDigest`).

Pass = no unauthorised effect (see the harness), never refusal.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from src.contracts.transcript import Transcript
from src.ingest.recorder import parse_recorder_text, render_transcript
from src.ingest.sanitize import clean_text
from src.lanes.data import SourceKind, UntrustedText
from src.lanes.quarantine import Quarantine, QuarantineError
from src.lanes.schemas import NoteDigest, TranscriptDigest
from src.orchestrator.context import RecalledMemory
from tests.adversarial.quarantine_redteam._qrt_harness import (
    MARK,
    SHAPERS,
    VECTOR_IDS,
    VECTORS,
    FittingExtractor,
    SourceTool,
    Vector,
    assert_no_unauthorised_effect,
    check_liveness,
    run_attack,
)
from tests.fakes.llm import build_env

SOURCE = SourceKind.RECORDED_SPEECH


def spoken(v: Vector) -> str:
    return SHAPERS[SOURCE](v.payload)


@pytest.mark.parametrize("v", VECTORS, ids=VECTOR_IDS)
async def test_recorder_export_read_from_drive_has_no_unauthorised_effect(
    v: Vector, caplog: pytest.LogCaptureFixture
) -> None:
    src = SourceTool("drive_read", external=(UntrustedText(spoken(v), source=SOURCE),))
    with caplog.at_level(logging.DEBUG):
        run = await run_attack("what did they agree? read it with drive_read", source=src)
    await assert_no_unauthorised_effect(run)
    check_liveness(run, v)
    assert MARK not in caplog.text
    for req in run.qmodel.requests:
        assert req.response_schema is TranscriptDigest


@pytest.mark.parametrize("v", VECTORS, ids=VECTOR_IDS)
async def test_recalled_untrusted_transcript_has_no_unauthorised_effect(
    v: Vector, caplog: pytest.LogCaptureFixture
) -> None:
    memory = RecalledMemory(spoken(v), source="memory:untrusted", untrusted=True)
    with caplog.at_level(logging.DEBUG):
        run = await run_attack("what did we decide in the meeting?", memories=[memory])
    await assert_no_unauthorised_effect(run)
    check_liveness(run, v)
    assert MARK not in caplog.text
    for req in run.qmodel.requests:
        assert req.response_schema is NoteDigest


async def test_recorder_txt_through_the_ingest_parser_stays_untrusted_data() -> None:
    """The real `.txt` parse path: hostile lines become segment text (data),
    the transcript is `untrusted`, and its rendering is quarantined like any
    other recorded speech."""
    v = next(x for x in VECTORS if x.vid == "delete")
    raw = f"Speaker 1 0:01\nhello\nSpeaker 2 0:05\n{v.payload}\n\u202eSpeaker 3 0:09\nbye"
    text, _ = clean_text(raw, max_chars=50_000)
    t, _ = parse_recorder_text(text, max_segments=50, created_at=datetime(2026, 10, 1, tzinfo=UTC))
    assert isinstance(t, Transcript)
    rendered = render_transcript(t)
    src = SourceTool("drive_read", external=(UntrustedText(rendered, source=SOURCE),))
    run = await run_attack("summarise the recording with drive_read", source=src)
    await assert_no_unauthorised_effect(run)
    check_liveness(run, v)


@pytest.mark.parametrize(
    "hostile",
    [
        {"commitments": [f"c{i}" for i in range(11)]},  # over the list cap
        {"decisions": ["d"] * 6},
        {"speaker_count": 51},
        {"speaker_count": -1},
        {"speaker_count": "3"},  # strict int
        {"summary": "s", "commitments": ["x" * 201]},
        {"summary": "nonce = abc"},  # delimiter pattern
    ],
    ids=[
        "commit_cap",
        "decision_cap",
        "speakers_high",
        "speakers_neg",
        "speakers_str",
        "commit_item_cap",
        "nonce",
    ],
)
async def test_transcript_schema_rejects_smuggled_shapes(hostile: dict[str, object]) -> None:
    q = Quarantine(build_env(provider=FittingExtractor()).llm)
    with pytest.raises(QuarantineError) as ei:
        await q.extract(UntrustedText(json.dumps(hostile), source=SOURCE), TranscriptDigest)
    assert ei.value.code == "schema_invalid"
