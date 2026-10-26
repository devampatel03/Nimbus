# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Fakes for the ingest tests. One shared `calls` log lets tests assert the
relative order of sink writes, gateway acks and network fetches."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from src.contracts.event import Event, EventKind
from src.gateway.types import (
    ApprovalCard,
    FileRef,
    FileTooLarge,
    InboundUpdate,
    MessageRef,
    UpdateKind,
)
from src.ingest.protocols import PendingFollowOn, TranscriptionResult

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


class Clock:
    """Mutable test clock (module-level `CLK`; tests reset it via fixture)."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


CLK = Clock()
clock = CLK


class FakeSink:
    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls = calls if calls is not None else []
        self.rows: dict[str, tuple[uuid.UUID, Event]] = {}
        self.attempts: dict[uuid.UUID, int] = {}
        self.fail_next = False

    async def record(self, event: Event) -> tuple[uuid.UUID, bool]:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("db down")
        self.calls.append(f"record:{event.source_key}")
        if event.source_key in self.rows:
            return self.rows[event.source_key][0], False
        eid = uuid.uuid4()
        self.rows[event.source_key] = (eid, event)
        return eid, True

    async def get_id(self, source_key: str) -> uuid.UUID | None:
        row = self.rows.get(source_key)
        return row[0] if row else None

    async def bump_attempts(self, event_id: uuid.UUID) -> int:
        self.attempts[event_id] = self.attempts.get(event_id, 0) + 1
        return self.attempts[event_id]

    async def pending_follow_ons(
        self, limit: int, *, older_than: datetime
    ) -> list[PendingFollowOn]:
        out = []
        for key, (eid, ev) in self.rows.items():
            if ev.kind not in (EventKind.VOICE, EventKind.RECORDER):
                continue
            if f"{key}:transcript" in self.rows or ev.occurred_at > older_than:
                continue
            if not isinstance(ev.meta.get("file_id"), str) or ev.file_unique_id is None:
                continue
            out.append(
                PendingFollowOn(
                    eid,
                    ev.kind,
                    ev.lane,
                    dict(ev.meta),
                    ev.file_unique_id,
                    ev.occurred_at,
                    self.attempts.get(eid, 0),
                )
            )
        out.sort(key=lambda p: (p.attempts, p.occurred_at))
        return out[:limit]


class FakeGateway:
    """Implements the slice of `Gateway` that ingest uses."""

    def __init__(
        self,
        calls: list[str],
        updates: list[InboundUpdate] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.calls = calls
        self.updates = updates or []
        self.files = files or {}
        self.acked: list[tuple[int, uuid.UUID | None]] = []
        self.sent: list[str] = []
        self.fetch_error: Exception | None = None

    async def inbound(self) -> AsyncIterator[InboundUpdate]:
        for u in self.updates:
            yield u

    async def ack(self, update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None:
        self.calls.append(f"ack:{update.update_id}")
        self.acked.append((update.update_id, event_id))

    async def send_text(self, text: str, *, reply_to: MessageRef | None = None) -> list[MessageRef]:
        self.sent.append(text)
        return []

    async def send_approval(self, card: ApprovalCard) -> MessageRef:
        raise NotImplementedError

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None:
        raise NotImplementedError

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        raise NotImplementedError

    async def fetch_file(self, file: FileRef, *, max_bytes: int) -> bytes:
        self.calls.append(f"fetch:{file.file_unique_id}")
        if self.fetch_error is not None:
            raise self.fetch_error
        data = self.files[file.file_id]
        if len(data) > max_bytes:
            raise FileTooLarge("file too large")
        return data


class FakeTranscriber:
    def __init__(self, calls: list[str], text: str = "hello from a voice note") -> None:
        self.calls = calls
        self.text = text
        self.error: Exception | None = None

    async def transcribe(self, audio: bytes, *, mime_type: str | None) -> TranscriptionResult:
        self.calls.append("transcribe")
        if self.error is not None:
            raise self.error
        return TranscriptionResult(text=self.text, model="fake-asr", language="en")


def text_update(uid: int, text: str = "hi", *, untrusted: bool = False) -> InboundUpdate:
    return InboundUpdate(uid, UpdateKind.TEXT, message_id=uid * 10, text=text, untrusted=untrusted)


def voice_update(
    uid: int, *, size: int | None = 100, mime: str | None = "audio/ogg", untrusted: bool = False
) -> InboundUpdate:
    ref = FileRef(f"fid{uid}", f"uniq{uid}", size, mime, None, UpdateKind.VOICE)
    return InboundUpdate(uid, UpdateKind.VOICE, message_id=uid * 10, file=ref, untrusted=untrusted)


def doc_update(
    uid: int, *, size: int | None = 100, name: str | None = "rec.txt", caption: str | None = None
) -> InboundUpdate:
    ref = FileRef(f"fid{uid}", f"uniq{uid}", size, "text/plain", name, UpdateKind.DOCUMENT)
    return InboundUpdate(
        uid, UpdateKind.DOCUMENT, message_id=uid * 10, text=caption, file=ref, untrusted=True
    )


def meta_of(sink: FakeSink, key: str) -> dict[str, Any]:
    return sink.rows[key][1].meta
