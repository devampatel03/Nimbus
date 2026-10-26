# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Ingest pipeline: gateway `InboundUpdate` -> `Event` rows.

Two phases, deliberately separate (gateway review, ADR-0016):

1. `store(update)` builds the Event from the update alone and records it. It
   performs NO network I/O (no download, no transcription), so the gateway ack
   never waits on the network. Idempotent on `source_key =
   "telegram:<update_id>"`: a redelivered update returns the existing event.
2. `follow_on(...)` runs AFTER the ack: download the voice note / Recorder
   document, validate, transcribe or parse, and store a *second* event
   `telegram:<update_id>:transcript` (events are append-only, so the first row
   is never updated). Own idempotency: if that event exists, it is a no-op. A
   permanent rejection stores a tombstone event (body None, `meta.rejected`) so
   a bad file is not re-downloaded forever. A crash between ack and follow-on,
   or a transient failure, is repaired by `recover_pending` (it needs
   `meta.file_id`, kept on the first event for exactly that reason).

INV-6: forwards / via_bot (`update.untrusted`) and every Recorder document are
`lane="untrusted"`; the transcript event inherits the parent's lane and can
never be more trusted than it. Nothing here promotes anything.
INV-7: logs carry update ids and fixed codes only, never text, file names or
transcripts. Untrusted content is never executed or rendered.
"""

from __future__ import annotations

import enum
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from src.contracts.event import Event, EventKind
from src.contracts.transcript import Segment, Transcript
from src.gateway.protocols import Gateway
from src.gateway.types import FileRef, FileTooLarge, InboundUpdate, UpdateKind
from src.ingest.protocols import (
    EventSink,
    Transcriber,
    TranscriptionFailed,
    TranscriptionUnavailable,
)
from src.ingest.recorder import parse_recorder_text
from src.ingest.sanitize import clean_label, clean_mime, clean_text, decode_text_document

log = logging.getLogger(__name__)

SOURCE = "telegram"
_VOICE_MIMES = frozenset({"audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4", "audio/x-m4a"})
_DEFAULT_ASR_CONFIDENCE = 0.9

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")  # Telegram file ids

_NOTICE_REJECTED = "I could not use that file (unsupported or too large)."
_NOTICE_FAILED = (
    "I could not process that message just now. I will retry automatically; "
    "resend it only if I report a failure again."
)
_NOTICE_EXHAUSTED = "I could not process that message after several tries. Please resend it."


@dataclass(frozen=True, slots=True)
class IngestSettings:
    max_text_chars: int = 16_000
    max_body_chars: int = 200_000
    max_voice_bytes: int = 5 * 1024 * 1024
    max_document_bytes: int = 512 * 1024
    max_segments: int = 2_000
    max_attempts: int = 3  # follow-on tries (first + recoveries) before a tombstone
    min_recover_age_s: float = 600.0  # do not race run(): parent must be this old
    max_recover_age_s: float = 86_400.0  # older unfinished parents are tombstoned


class Outcome(enum.Enum):
    STORED = "stored"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"  # content refused (type, size, encoding); permanent
    FAILED = "failed"  # transient (download/transcription); retryable via recover


@dataclass(frozen=True, slots=True)
class StoreResult:
    event_id: uuid.UUID
    created: bool
    needs_follow_on: bool


@dataclass(frozen=True, slots=True)
class FollowOnResult:
    outcome: Outcome
    reason: str | None = None
    event_id: uuid.UUID | None = None


def transcript_key(source_key: str) -> str:
    return f"{source_key}:transcript"


def transcript_from_event(event: Event) -> Transcript | None:
    """Rebuild the typed `Transcript` from a stored transcript event (input to
    the extractor). Lane untrusted => `Transcript.untrusted`. A tombstone (no
    body, `meta.rejected`) has nothing to extract: returns None, so callers
    make no model call for it."""
    if event.body is None and "rejected" in event.meta:
        return None
    untrusted = event.lane == "untrusted"
    body = event.body or ""
    if event.kind is EventKind.RECORDER_TRANSCRIPT:
        t, _ = parse_recorder_text(body, max_segments=2_000, created_at=event.occurred_at)
        return t
    lang = event.meta.get("language")
    return Transcript(
        segments=[Segment(text=body)] if body else [],
        source_model=str(event.meta.get("asr_model") or "unknown"),
        language=lang if isinstance(lang, str) else None,
        untrusted=untrusted,
        created_at=event.occurred_at,
    )


class Ingestor:
    def __init__(
        self,
        sink: EventSink,
        gateway: Gateway,
        transcriber: Transcriber,
        settings: IngestSettings | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sink = sink
        self._gw = gateway
        self._asr = transcriber
        self._cfg = settings or IngestSettings()
        self._clock = clock
        self._inflight: set[uuid.UUID] = set()

    # -- phase 1: no network ------------------------------------------------

    async def store(self, update: InboundUpdate) -> StoreResult | None:
        """None => nothing to store (callback, blank text): the caller just acks."""
        event = self._build_event(update)
        if event is None:
            return None
        event_id, created = await self._sink.record(event)
        return StoreResult(
            event_id,
            created,
            needs_follow_on=update.kind in (UpdateKind.VOICE, UpdateKind.DOCUMENT),
        )

    def _build_event(self, u: InboundUpdate) -> Event | None:
        key = f"{SOURCE}:{u.update_id}"
        now = self._clock()
        meta: dict[str, Any] = {"update_id": u.update_id}
        if u.message_id is not None:
            meta["message_id"] = u.message_id
        if u.kind is UpdateKind.TEXT:
            body, trunc = clean_text(u.text or "", max_chars=self._cfg.max_text_chars)
            if not body.strip():
                return None
            if trunc:
                meta["truncated"] = True
            if u.untrusted:
                meta["untrusted_reason"] = "forwarded_or_via_bot"
            return Event(
                source_key=key,
                kind=EventKind.TEXT,
                source=SOURCE,
                lane="untrusted" if u.untrusted else "trusted",
                body=body,
                meta=meta,
                occurred_at=now,
            )
        if u.kind in (UpdateKind.VOICE, UpdateKind.DOCUMENT) and u.file is not None:
            f = u.file
            if not _ID_RE.fullmatch(f.file_id) or not _ID_RE.fullmatch(f.file_unique_id):
                log.warning("ingest rejected malformed file id update=%s", u.update_id)
                return None
            meta.update(
                file_id=f.file_id,
                size=f.size if isinstance(f.size, int) else None,
                mime_type=clean_mime(f.mime_type),
            )
            is_doc = u.kind is UpdateKind.DOCUMENT
            caption: str | None = None
            if is_doc:
                meta["file_name"] = clean_label(f.file_name)
                meta["untrusted_reason"] = "third_party_speech"
                caption, _ = clean_text(u.text or "", max_chars=self._cfg.max_text_chars)
            elif u.untrusted:
                meta["untrusted_reason"] = "forwarded_or_via_bot"
            return Event(
                source_key=key,
                kind=EventKind.RECORDER if is_doc else EventKind.VOICE,
                source=SOURCE,
                lane="untrusted" if (u.untrusted or is_doc) else "trusted",
                body=caption or None,
                meta=meta,
                file_unique_id=f.file_unique_id,
                occurred_at=now,
            )
        return None

    # -- phase 2: network, after ack ---------------------------------------

    async def follow_on(
        self,
        *,
        update_id: int,
        file: FileRef,
        parent_event_id: uuid.UUID,
        untrusted: bool,
        expired: bool = False,
    ) -> FollowOnResult:
        self._inflight.add(parent_event_id)
        try:
            return await self._follow_on(update_id, file, parent_event_id, untrusted, expired)
        finally:
            self._inflight.discard(parent_event_id)

    async def _follow_on(
        self,
        update_id: int,
        file: FileRef,
        parent_event_id: uuid.UUID,
        untrusted: bool,
        expired: bool,
    ) -> FollowOnResult:
        parent_key = f"{SOURCE}:{update_id}"
        tkey = transcript_key(parent_key)
        if (existing := await self._sink.get_id(tkey)) is not None:
            return FollowOnResult(Outcome.DUPLICATE, event_id=existing)

        is_voice = file.kind is UpdateKind.VOICE
        kind = EventKind.VOICE_TRANSCRIPT if is_voice else EventKind.RECORDER_TRANSCRIPT
        lane: Literal["trusted", "untrusted"] = (
            "untrusted" if (untrusted or not is_voice) else "trusted"
        )  # INV-6
        meta: dict[str, Any] = {
            "parent_source_key": parent_key,
            "parent_event_id": str(parent_event_id),
        }

        async def reject(reason: str) -> FollowOnResult:
            tomb = Event(
                source_key=tkey,
                kind=kind,
                source=SOURCE,
                lane="untrusted",
                confidence=0.0,
                body=None,
                meta={**meta, "rejected": reason},
                file_unique_id=file.file_unique_id,
                occurred_at=self._clock(),
            )
            eid, _ = await self._sink.record(tomb)
            return FollowOnResult(Outcome.REJECTED, reason, eid)

        async def failed(reason: str) -> FollowOnResult:
            n = await self._sink.bump_attempts(parent_event_id)
            if n >= self._cfg.max_attempts:
                return await reject("retries_exhausted")
            return FollowOnResult(Outcome.FAILED, reason)

        if expired:
            return await reject("retries_exhausted")
        cap = self._cfg.max_voice_bytes if is_voice else self._cfg.max_document_bytes
        if file.size is not None and file.size > cap:
            return await reject("too_large")
        mime = clean_mime(file.mime_type)
        if is_voice and file.mime_type and mime not in _VOICE_MIMES:
            return await reject("bad_mime")

        try:
            data = await self._gw.fetch_file(file, max_bytes=cap)
        except FileTooLarge:
            return await reject("too_large")
        except Exception as exc:  # noqa: BLE001 - boundary: any download failure is retryable
            log.warning("ingest download failed update=%s err=%s", update_id, type(exc).__name__)
            return await failed("download_failed")
        if len(data) > cap:
            return await reject("too_large")

        now = self._clock()
        if is_voice:
            try:
                res = await self._asr.transcribe(data, mime_type=mime)
            except TranscriptionUnavailable as exc:
                return await reject(exc.code)
            except TranscriptionFailed as exc:
                return await failed(exc.code)
            except Exception as exc:  # noqa: BLE001 - boundary: provider errors retryable
                log.warning("ingest asr error update=%s err=%s", update_id, type(exc).__name__)
                return await failed("transcription_failed")
            body, trunc = clean_text(res.text, max_chars=self._cfg.max_body_chars)
            if not body.strip():
                return await reject("empty_transcript")
            meta.update(
                asr_model=clean_label(res.model),
                language=clean_label(res.language, max_chars=16),
                truncated=trunc,
            )
            conf = res.confidence if res.confidence is not None else _DEFAULT_ASR_CONFIDENCE
            conf = min(1.0, max(0.0, conf))
        else:
            raw = decode_text_document(data)
            if raw is None:
                return await reject("not_text")
            body, trunc = clean_text(raw, max_chars=self._cfg.max_body_chars)
            if not body.strip():
                return await reject("empty_document")
            parsed, seg_trunc = parse_recorder_text(
                body, max_segments=self._cfg.max_segments, created_at=now
            )
            meta.update(segment_count=len(parsed.segments), truncated=trunc or seg_trunc)
            conf = 1.0
        event = Event(
            source_key=tkey,
            kind=kind,
            source=SOURCE,
            lane=lane,
            confidence=conf,
            body=body,
            meta=meta,
            file_unique_id=file.file_unique_id,
            occurred_at=now,
        )
        event_id, created = await self._sink.record(event)
        return FollowOnResult(Outcome.STORED if created else Outcome.DUPLICATE, event_id=event_id)

    async def recover_pending(self, limit: int = 20) -> list[FollowOnResult]:
        """Re-run follow-ons whose first event exists but whose transcript
        event does not (crash after ack, or a transient failure).

        Bounded: only parents at least `min_recover_age_s` old (so a pass cannot
        race `run()`), none currently in flight, fewest attempts first (failing
        items cannot starve fresh ones), each tried at most `max_attempts`
        times, and parents older than `max_recover_age_s` get a permanent
        `retries_exhausted` tombstone without any download.
        """
        now = self._clock()
        older_than = now - timedelta(seconds=self._cfg.min_recover_age_s)
        out: list[FollowOnResult] = []
        for p in await self._sink.pending_follow_ons(limit, older_than=older_than):
            fid, uid = p.meta.get("file_id"), p.meta.get("update_id")
            if p.id in self._inflight:
                continue
            if not isinstance(fid, str) or not isinstance(uid, int) or p.file_unique_id is None:
                continue
            size, mime = p.meta.get("size"), p.meta.get("mime_type")
            ref = FileRef(
                file_id=fid,
                file_unique_id=p.file_unique_id,
                size=size if isinstance(size, int) else None,
                mime_type=mime if isinstance(mime, str) else None,
                file_name=None,
                kind=UpdateKind.VOICE if p.kind is EventKind.VOICE else UpdateKind.DOCUMENT,
            )
            age = (now - p.occurred_at).total_seconds()
            res = await self.follow_on(
                update_id=uid,
                file=ref,
                parent_event_id=p.id,
                untrusted=p.lane == "untrusted",
                expired=age > self._cfg.max_recover_age_s or p.attempts >= self._cfg.max_attempts,
            )
            out.append(res)
            if res.outcome is Outcome.REJECTED:
                await self._notify(res)
        return out

    async def _notify(self, fr: FollowOnResult) -> None:
        if fr.outcome is Outcome.FAILED:
            text = _NOTICE_FAILED
        elif fr.reason == "retries_exhausted":
            text = _NOTICE_EXHAUSTED
        else:
            text = _NOTICE_REJECTED
        log.info("ingest follow-on %s reason=%s", fr.outcome.value, fr.reason)
        try:
            await self._gw.send_text(text)
        except Exception as exc:  # noqa: BLE001 - a lost notice must not stop ingest
            log.warning("ingest notice failed err=%s", type(exc).__name__)

    # -- loop ---------------------------------------------------------------

    async def run(self) -> None:
        """Consume the gateway: store -> ack -> follow-on, one update at a time.
        A `store` failure propagates WITHOUT ack, so the update is redelivered."""
        async for update in self._gw.inbound():
            res = await self.store(update)
            await self._gw.ack(update, event_id=res.event_id if res else None)
            if res is None:
                if update.kind in (UpdateKind.VOICE, UpdateKind.DOCUMENT):
                    await self._notify(FollowOnResult(Outcome.REJECTED, "malformed"))
                continue
            if not res.needs_follow_on or update.file is None:
                continue
            fr = await self.follow_on(
                update_id=update.update_id,
                file=update.file,
                parent_event_id=res.event_id,
                untrusted=update.untrusted,
            )
            if fr.outcome in (Outcome.REJECTED, Outcome.FAILED):
                await self._notify(fr)
