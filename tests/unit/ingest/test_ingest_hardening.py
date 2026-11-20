# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Telegram-supplied metadata validation, Event meta cap, tombstone handling,
INV-6 construction ban."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.contracts.event import Event, EventKind
from src.gateway.types import FileRef, InboundUpdate, UpdateKind
from src.ingest import Ingestor, transcript_from_event
from tests.unit.ingest.fakes import (
    CLK,
    FakeGateway,
    FakeSink,
    FakeTranscriber,
    voice_update,
)


def make():
    calls: list[str] = []
    sink = FakeSink(calls)
    gw = FakeGateway(calls, [], {})
    return Ingestor(sink, gw, FakeTranscriber(calls), clock=CLK), sink, gw


def upd(file_id: str = "fid1", uniq: str = "u1", mime: str | None = "audio/ogg") -> InboundUpdate:
    ref = FileRef(file_id, uniq, 10, mime, None, UpdateKind.VOICE)
    return InboundUpdate(1, UpdateKind.VOICE, message_id=1, file=ref)


@pytest.mark.parametrize(
    ("fid", "uniq"),
    [("a b", "u"), ("a" * 257, "u"), ("", "u"), ("ok", "x/../y"), ("ok", "u\n"), ("\x00", "u")],
)
async def test_bad_file_ids_are_rejected_not_stored(fid: str, uniq: str) -> None:
    ing, sink, gw = make()
    gw.updates = [upd(fid, uniq)]
    await ing.run()
    assert not sink.rows
    assert gw.acked and gw.acked[0][1] is None  # acked so it is not redelivered forever
    assert gw.sent and "could not use" in gw.sent[0]


async def test_huge_mime_type_is_capped_in_meta() -> None:
    ing, sink, _ = make()
    await ing.store(upd(mime="audio/" + "x" * 2_000_000))
    mime = sink.rows["telegram:1"][1].meta["mime_type"]
    assert mime is None or len(mime) <= 100


async def test_nul_and_surrogate_in_mime_are_cleaned() -> None:
    ing, sink, _ = make()
    await ing.store(upd(mime="audio/\x00og\ud800g"))
    mime = sink.rows["telegram:1"][1].meta["mime_type"]
    assert mime is None or ("\x00" not in mime and "\ud800" not in mime)


def test_event_meta_size_is_capped() -> None:
    with pytest.raises(ValueError):
        Event(
            source_key="k",
            kind=EventKind.TEXT,
            source="telegram",
            lane="trusted",
            meta={"blob": "x" * 20_000},
            occurred_at=CLK(),
        )


def test_transcript_from_tombstone_is_none() -> None:
    tomb = Event(
        source_key="telegram:1:transcript",
        kind=EventKind.VOICE_TRANSCRIPT,
        source="telegram",
        lane="untrusted",
        confidence=0.0,
        meta={"rejected": "bad_mime"},
        occurred_at=CLK(),
    )
    assert transcript_from_event(tomb) is None


def test_inv6_event_never_built_via_model_construct_or_lane_copy() -> None:
    root = Path(__file__).parents[3] / "src"
    bad: list[str] = []
    for f in root.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf8"))):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr == "model_construct":
                bad.append(f"{f.name}:{n.lineno} model_construct")
            if n.func.attr == "model_copy":
                for kw in n.keywords:
                    if kw.arg == "update" and isinstance(kw.value, ast.Dict):
                        keys = {k.value for k in kw.value.keys if isinstance(k, ast.Constant)}
                        if "lane" in keys:
                            bad.append(f"{f.name}:{n.lineno} model_copy(lane)")
    assert not bad, bad


def test_voice_update_helper_still_valid() -> None:
    assert voice_update(1).file is not None
