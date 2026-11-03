# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.1 red team, category forwarded Telegram message (INV-5, INV-6, R8).

A message forwarded into the chat (or relayed by a bot) is third-party text.
Two gates:

* it never STARTS a turn: `parse_update` marks it untrusted, ingest stores it
  in the untrusted lane, and `TurnGateway.ack` queues no turn for it, whatever
  it says;
* recalled later as memory it is quarantined (`UNTRUSTED_MEMORY` ->
  `NoteDigest`) and every attack in `_qrt_harness.VECTORS` has no
  unauthorised effect (see the harness), never refusal.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from src.app.fakes import InMemoryGateway
from src.app.pipeline import PendingTurn, TurnGateway
from src.contracts.event import Event
from src.gateway.parse import parse_update
from src.gateway.types import UpdateKind
from src.ingest.pipeline import Ingestor
from src.ingest.protocols import UnavailableTranscriber
from src.lanes.data import SourceKind
from src.lanes.schemas import NoteDigest
from src.orchestrator.context import RecalledMemory
from tests.adversarial.quarantine_redteam._qrt_harness import (
    MARK,
    SHAPERS,
    VECTOR_IDS,
    VECTORS,
    Vector,
    assert_no_unauthorised_effect,
    check_liveness,
    run_attack,
)

SOURCE = SourceKind.FORWARDED_MESSAGE
USER = 424_242
ATTACK = next(v for v in VECTORS if v.vid == "delete").payload


@pytest.mark.parametrize("v", VECTORS, ids=VECTOR_IDS)
async def test_recalled_forward_has_no_unauthorised_effect(
    v: Vector, caplog: pytest.LogCaptureFixture
) -> None:
    memory = RecalledMemory(SHAPERS[SOURCE](v.payload), source="memory:untrusted", untrusted=True)
    with caplog.at_level(logging.DEBUG):
        run = await run_attack("what was that message I forwarded about?", memories=[memory])
    await assert_no_unauthorised_effect(run)
    check_liveness(run, v)
    assert MARK not in caplog.text
    for req in run.qmodel.requests:
        assert req.response_schema is NoteDigest


def _raw(extra: dict[str, Any], text: str = ATTACK) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": 7,
        "from": {"id": USER, "is_bot": False},
        "chat": {"id": USER, "type": "private"},
        "date": 1,
        "text": text,
    }
    msg.update(extra)
    return {"update_id": 99, "message": msg}


FORWARD_MARKERS: dict[str, dict[str, Any]] = {
    "forward_origin_user": {"forward_origin": {"type": "user", "date": 1}},
    "forward_origin_hidden": {"forward_origin": {"type": "hidden_user", "sender_user_name": "x"}},
    "forward_origin_channel": {"forward_origin": {"type": "channel", "chat": {"id": -100}}},
    "forward_date_only": {"forward_date": 1},
    "forward_from": {"forward_from": {"id": 1, "is_bot": False}},
    "forward_from_chat": {"forward_from_chat": {"id": -100, "type": "channel"}},
    "forward_sender_name": {"forward_sender_name": "Bank Support"},
    "via_bot": {"via_bot": {"id": 5, "is_bot": True}},
    "automatic_forward": {"is_automatic_forward": True},
}


class _Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def record(self, event: Event) -> tuple[uuid.UUID, bool]:
        self.events.append(event)
        return uuid.uuid4(), True


@pytest.mark.parametrize("marker", sorted(FORWARD_MARKERS))
async def test_forwarded_text_never_starts_a_turn_and_is_stored_untrusted(marker: str) -> None:
    parsed = parse_update(_raw(FORWARD_MARKERS[marker]), USER)
    assert parsed is not None and parsed.update is not None
    update = parsed.update
    assert update.kind is UpdateKind.TEXT and update.untrusted is True
    sink = _Sink()
    ingest = Ingestor(
        sink,  # type: ignore[arg-type]
        InMemoryGateway(),
        UnavailableTranscriber(),
        clock=lambda: datetime(2026, 10, 1, tzinfo=UTC),
    )
    stored = await ingest.store(update)
    assert stored is not None
    (event,) = sink.events
    assert event.lane == "untrusted"
    assert event.meta.get("untrusted_reason") == "forwarded_or_via_bot"
    queue: asyncio.Queue[PendingTurn] = asyncio.Queue()
    await TurnGateway(InMemoryGateway(), queue).ack(update, event_id=stored.event_id)
    assert queue.empty(), "a forwarded message started a turn"


async def test_users_own_text_does_start_a_turn() -> None:
    """Control: the same text typed by the user is a trusted instruction
    (the gate above is what stops the forward, not the content)."""
    parsed = parse_update(_raw({}), USER)
    assert parsed is not None and parsed.update is not None
    assert parsed.update.untrusted is False
    queue: asyncio.Queue[PendingTurn] = asyncio.Queue()
    await TurnGateway(InMemoryGateway(), queue).ack(parsed.update, event_id=uuid.uuid4())
    assert queue.qsize() == 1


async def test_forwarded_voice_note_is_untrusted_and_starts_no_turn() -> None:
    raw = _raw({"forward_origin": {"type": "user", "date": 1}}, text="")
    del raw["message"]["text"]
    raw["message"]["voice"] = {"file_id": "AwADBAAD1", "file_unique_id": "AgAD1", "duration": 3}
    parsed = parse_update(raw, USER)
    assert parsed is not None and parsed.update is not None
    assert parsed.update.kind is UpdateKind.VOICE and parsed.update.untrusted is True
    queue: asyncio.Queue[PendingTurn] = asyncio.Queue()
    await TurnGateway(InMemoryGateway(), queue).ack(parsed.update, event_id=uuid.uuid4())
    assert queue.empty()
