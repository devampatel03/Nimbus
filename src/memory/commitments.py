# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Commitment ledger service (P3.9, ADR-0022; INV-3, INV-5, INV-7, R7, R8).

"I'll send X by Friday" becomes a CANDIDATE row. It becomes `confirmed` only
through `confirm`, which needs the id of a consumed `commitment`/`confirm`
approval (user-bound, single-use, ADR-0020). The database re-checks that
(`nimbus_ledger_guard`), so a bug here cannot confirm anything either.

* Append-only: every transition inserts a new row (`version + 1`); nothing is
  updated or deleted.
* Provenance: the lane is READ FROM the source event, never from the caller, so
  a commitment derived from untrusted text is `untrusted` and stays a
  candidate until the user approves it (R8).
* The description is the model's PARAPHRASE of the user's message, not their
  verbatim words: the approval card shows it labelled untrusted, and the tap
  approves "track this", not the wording.
* No model output is trusted for time: the extractor supplies the user's own
  words for the deadline (`due_phrase`); code checks the phrase really occurs in
  the message and resolves it deterministically in Asia/Kolkata relative to the
  message time (ADR-0008). Anything ambiguous stays UNRESOLVED and the card says
  so; the model's own `due_at` is ignored.
* No LLM call here, no transport. Extraction is the existing guarded
  `CombinedExtractor` run by the `memory_extract` job.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy import and_, func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.core.timeparse import TimeParseError, format_ist, parse_when
from src.llm.scrub import redact_secrets
from src.memory.schemas.event import Event
from src.memory.schemas.ledger import CommitmentLedger

log = logging.getLogger(__name__)

MIN_CONFIDENCE: Final = 0.5
MAX_DESCRIPTION: Final = 500
MAX_PHRASE: Final = 60
MAX_PER_EVENT: Final = 3
OPEN_STATUSES: Final = ("candidate", "confirmed")
# Assumptions parse_when may make that we still accept as a resolved due time.
# Everything else (day-first numeric dates, invented years, rolled-over weekdays,
# "next Friday") is ambiguous: the due time stays unresolved and the user is asked.
_ACCEPTED_ASSUMPTIONS: Final = ("No time given",)

# Deterministic cue for a first-person commitment: lets a short message such as
# "I'll send the report by Friday" past the extractor's minimum-length gate.
_CUE: Final = re.compile(
    r"\b(i\s*'\s*ll|i\s+will|i\s*'?m\s+going\s+to|i\s+am\s+going\s+to|i\s+promise|i\s+shall)\b"
    r"|\bi’ll\b",
    re.IGNORECASE | re.ASCII,
)

_WS: Final = re.compile(r"\s+")
_PUNCT: Final = re.compile(r"[^\w\s]", re.UNICODE)


class LedgerRefused(Exception):
    """A transition the ledger will not make. `code` is fixed text (INV-7)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"commitment ledger refused: {code}")


def has_commitment_cue(text: str) -> bool:
    return bool(_CUE.search(unicodedata.normalize("NFKC", text)))


def _clean(text: str, limit: int) -> str:
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(ch for ch in folded if ch.isprintable() or ch in " \t\n")
    return _WS.sub(" ", redact_secrets(folded)).strip()[:limit]


def normalise(description: str) -> str:
    """Key for the duplicate check: case, punctuation and spacing do not matter."""
    folded = _PUNCT.sub("", unicodedata.normalize("NFKC", description).casefold())
    return hashlib.sha256(_WS.sub(" ", folded).strip().encode()).hexdigest()


@dataclass(frozen=True)
class Due:
    state: str  # none | resolved | unresolved
    at: datetime | None = None
    phrase: str | None = None
    note: str = ""


def resolve_due(phrase: str | None, *, body: str, event_time: datetime) -> Due:
    """The model only quotes the user's words; code decides what they mean."""
    if phrase is None or not phrase.strip():
        return Due("none")
    clean = _clean(phrase, MAX_PHRASE)
    if not clean:
        return Due("none")
    haystack = _WS.sub(" ", unicodedata.normalize("NFKC", body)).casefold()
    if clean.casefold() not in haystack:
        # Forged or hallucinated: the user never wrote this deadline.
        return Due("unresolved", None, None, "the deadline wording is not in your message")
    try:
        got = parse_when(clean, now=event_time)
    except TimeParseError as exc:
        return Due("unresolved", None, clean, f"could not pin down the date ({exc.code})")
    extra = [a for a in got.assumptions if not a.startswith(_ACCEPTED_ASSUMPTIONS)]
    if extra:
        return Due("unresolved", None, clean, "the date is ambiguous")
    note = " ".join(got.assumptions)
    return Due("resolved", got.at, clean, note)


@dataclass(frozen=True)
class Commitment:
    """A read-only view of one ledger row."""

    id: uuid.UUID
    key: uuid.UUID
    version: int
    status: str
    description: str
    due_at: datetime | None
    due_state: str
    due_phrase: str | None
    lane: str
    source_event_id: uuid.UUID
    confidence: float
    due_note: str = ""  # assumption made while resolving the due time (not stored)

    def preview(self) -> str:
        """Card text assembled by code from the stored row. The description is the
        model's PARAPHRASE of the user's message, not their verbatim words, so the
        approval card shows this whole block labelled untrusted."""
        lines = [f"Commitment: {self.description[:200]}"]
        if self.due_at is not None:
            lines.append(f"Due: {format_ist(self.due_at)}")
            if self.due_note:
                lines.append(f"Assumed: {self.due_note}")
        elif self.due_state == "unresolved":
            lines.append(
                "Due: NOT SET. I could not tell exactly when that means. Approve to track it "
                "without a date, or Reject it and send the commitment again with an exact "
                "date (for example 12 Nov, 5pm)."
            )
        else:
            lines.append("Due: no deadline mentioned.")
        if self.lane != "trusted":
            lines.append("Origin: UNTRUSTED text (not your own words).")
        return "\n".join(lines)


def _view(r: CommitmentLedger) -> Commitment:
    return Commitment(
        id=r.id,
        key=r.commitment_key,
        version=r.version,
        status=r.status,
        description=r.description,
        due_at=r.due_at,
        due_state=r.due_state,
        due_phrase=r.due_phrase,
        lane=r.lane,
        source_event_id=r.source_event_id,
        confidence=r.confidence,
    )


class ConfirmationRequester(Protocol):
    """`ApprovalService.request_commitment_confirm` (kept as a protocol so
    `src.memory` does not import `src.approvals`)."""

    async def request_commitment_confirm(
        self, candidate_id: uuid.UUID, *, preview: str | None = None
    ) -> object: ...


class CommitmentLedgerStore:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], clock: Clock | None = None
    ) -> None:
        self._sf = sessions
        self._clock = clock

    # ------------------------------------------------------------- reads

    async def get(self, row_id: uuid.UUID) -> Commitment | None:
        async with self._sf() as s:
            row = await s.get(CommitmentLedger, row_id)
            return _view(row) if row is not None else None

    async def _latest(self, s: AsyncSession, key: uuid.UUID) -> CommitmentLedger | None:
        got: CommitmentLedger | None = await s.scalar(
            select(CommitmentLedger)
            .where(CommitmentLedger.commitment_key == key)
            .order_by(CommitmentLedger.version.desc())
            .limit(1)
        )
        return got

    async def current(self, key: uuid.UUID) -> Commitment | None:
        async with self._sf() as s:
            row = await self._latest(s, key)
            return _view(row) if row is not None else None

    def _latest_stmt(self, *statuses: str) -> object:
        newer = CommitmentLedger.__table__.alias("newer")
        return (
            select(CommitmentLedger)
            .where(
                CommitmentLedger.status.in_(statuses),
                ~select(newer.c.id).where(newer.c.supersedes_id == CommitmentLedger.id).exists(),
            )
            .order_by(CommitmentLedger.created_at)
        )

    async def card_preview(self, c: Commitment) -> str:
        """`preview` plus the assumption behind the due time, re-derived by code from
        the source event (deterministic, so a re-asked card reads like the first)."""
        note = ""
        if c.due_state == "resolved" and c.due_at is not None and c.due_phrase:
            async with self._sf() as s:
                ev = (
                    await s.execute(
                        select(Event.body, Event.occurred_at).where(Event.id == c.source_event_id)
                    )
                ).one_or_none()
            if ev is not None:
                due = resolve_due(c.due_phrase, body=ev.body or "", event_time=ev.occurred_at)
                if due.state == "resolved" and due.at == c.due_at:
                    note = due.note
        return replace(c, due_note=note).preview()

    async def duplicate_of(self, description: str) -> Commitment | None:
        """The open (candidate or confirmed) row with the same normalised description."""
        desc = _clean(description, MAX_DESCRIPTION)
        if not desc:
            return None
        async with self._sf() as s:
            dup = await s.scalar(
                self._latest_stmt(*OPEN_STATUSES)  # type: ignore[attr-defined]
                .where(CommitmentLedger.norm_key == normalise(desc))
                .limit(1)
            )
            return _view(dup) if dup is not None else None

    async def review_queue(self, *, limit: int = 50) -> list[Commitment]:
        """Candidates still awaiting the user's decision (R8 review queue)."""
        async with self._sf() as s:
            rows = await s.scalars(self._latest_stmt("candidate").limit(limit))  # type: ignore[attr-defined]
            return [_view(r) for r in rows]

    async def open_confirmed(
        self, *, due_before: datetime | None = None, limit: int = 100
    ) -> list[Commitment]:
        """Hook for P5.1 nudges: confirmed, not yet completed or cancelled."""
        async with self._sf() as s:
            stmt = self._latest_stmt("confirmed")
            if due_before is not None:
                stmt = stmt.where(  # type: ignore[attr-defined]
                    and_(
                        CommitmentLedger.due_at.is_not(None), CommitmentLedger.due_at <= due_before
                    )
                )
            rows = await s.scalars(stmt.limit(limit))  # type: ignore[attr-defined]
            return [_view(r) for r in rows]

    # ------------------------------------------------------------ writes

    async def propose(
        self,
        event_id: uuid.UUID,
        *,
        description: str,
        confidence: float,
        due_phrase: str | None = None,
    ) -> Commitment | None:
        """Insert a CANDIDATE (never confirmed). None when it is a duplicate, too
        weak or empty. Replaying the same event and description is a no-op."""
        desc = _clean(description, MAX_DESCRIPTION)
        if not desc or not (MIN_CONFIDENCE <= confidence <= 1.0):
            return None
        norm = normalise(desc)
        async with self._sf() as s:
            ev = (
                await s.execute(
                    select(Event.lane, Event.body, Event.occurred_at).where(Event.id == event_id)
                )
            ).one_or_none()
            if ev is None:
                raise LedgerRefused("event_missing")
            count = await s.scalar(
                select(func.count())
                .select_from(CommitmentLedger)
                .where(CommitmentLedger.source_event_id == event_id, CommitmentLedger.version == 1)
            )
            if (count or 0) >= MAX_PER_EVENT:
                return None
            dup = await s.scalar(
                self._latest_stmt(*OPEN_STATUSES)  # type: ignore[attr-defined]
                .where(CommitmentLedger.norm_key == norm)
                .limit(1)
            )
            if dup is not None:
                return None
            due = resolve_due(due_phrase, body=ev.body or "", event_time=ev.occurred_at)
            row = CommitmentLedger(
                id=uuid.uuid4(),
                commitment_key=uuid.uuid4(),
                version=1,
                supersedes_id=None,
                status="candidate",
                description=desc,
                due_at=due.at,
                due_state=due.state,
                due_phrase=due.phrase,
                norm_key=norm,
                dedupe_key=f"c1:{event_id}:{norm}",
                source_event_id=event_id,
                lane=ev.lane,  # from the event, never from the caller (R8)
                confidence=confidence,
                promoted=False,
            )
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return None  # same event + description already recorded
            return _view(row)

    async def _next(
        self,
        prev_id: uuid.UUID,
        status: str,
        *,
        source_event_id: uuid.UUID | None = None,
        approval_id: uuid.UUID | None = None,
        due: Due | None = None,
    ) -> Commitment:
        async with self._sf() as s:
            prev = await s.get(CommitmentLedger, prev_id)
            if prev is None:
                raise LedgerRefused("not_found")
            latest = await self._latest(s, prev.commitment_key)
            if latest is None or latest.id != prev.id:
                raise LedgerRefused("superseded")
            version = prev.version + 1
            new_due = due if due is not None else Due(prev.due_state, prev.due_at, prev.due_phrase)
            row = CommitmentLedger(
                id=uuid.uuid4(),
                commitment_key=prev.commitment_key,
                version=version,
                supersedes_id=prev.id,
                status=status,
                description=prev.description,
                due_at=new_due.at,
                due_state=new_due.state,
                due_phrase=new_due.phrase,
                norm_key=prev.norm_key,
                dedupe_key=f"c{version}:{prev.commitment_key}",
                source_event_id=source_event_id or prev.source_event_id,
                lane=prev.lane,
                confidence=prev.confidence,
                promoted=status == "confirmed",
                promoted_approval_id=approval_id,
            )
            s.add(row)
            try:
                await s.commit()
            except (IntegrityError, DBAPIError) as exc:
                await s.rollback()
                log.info("ledger transition refused status=%s err=%s", status, type(exc).__name__)
                raise LedgerRefused("db_refused") from None
            return _view(row)

    async def confirm(self, candidate_id: uuid.UUID, approval_id: uuid.UUID) -> Commitment:
        """Called only by the approval executor after `ApprovalStore.consume`.
        The DB trigger re-verifies the approval, so this is not the only guard."""
        return await self._next(candidate_id, "confirmed", approval_id=approval_id)

    async def _user_event(self, event_id: uuid.UUID) -> None:
        async with self._sf() as s:
            ev = (
                await s.execute(select(Event.lane, Event.kind).where(Event.id == event_id))
            ).one_or_none()
        if ev is None or ev.lane != "trusted" or ev.kind not in ("text", "voice_transcript"):
            raise LedgerRefused("needs_trusted_user_event")

    async def reject(self, candidate_id: uuid.UUID, *, event_id: uuid.UUID) -> Commitment:
        await self._user_event(event_id)
        return await self._next(candidate_id, "rejected", source_event_id=event_id)

    async def reject_via_card(self, candidate_id: uuid.UUID) -> Commitment:
        """The user tapped Reject on the card (a verified, user-bound callback).
        Only ever moves a candidate to `rejected`; it can never confirm (INV-5)."""
        return await self._next(candidate_id, "rejected")

    async def complete(self, row_id: uuid.UUID, *, event_id: uuid.UUID) -> Commitment:
        await self._user_event(event_id)
        return await self._next(row_id, "completed", source_event_id=event_id)

    async def cancel(self, row_id: uuid.UUID, *, event_id: uuid.UUID) -> Commitment:
        await self._user_event(event_id)
        return await self._next(row_id, "cancelled", source_event_id=event_id)

    async def set_due(
        self, candidate_id: uuid.UUID, phrase: str, *, event_id: uuid.UUID
    ) -> Commitment:
        """The user answers "when?": a new CANDIDATE version, still unconfirmed.
        The phrase must be in the user's own message and resolve unambiguously."""
        await self._user_event(event_id)
        async with self._sf() as s:
            ev = (
                await s.execute(select(Event.body, Event.occurred_at).where(Event.id == event_id))
            ).one()
        due = resolve_due(phrase, body=ev.body or "", event_time=ev.occurred_at)
        if due.state != "resolved":
            raise LedgerRefused("due_unresolved")
        return await self._next(candidate_id, "candidate", source_event_id=event_id, due=due)


class CommitmentIntake:
    """Glue used by the `memory_extract` job: record candidates, then ask the
    user to confirm each one. A card that cannot be sent now (cap reached, Telegram
    down) leaves the candidate in the review queue; `drain` asks again when a
    commitment card closes. Nothing is ever confirmed here."""

    def __init__(
        self, ledger: CommitmentLedgerStore, requester: ConfirmationRequester | None = None
    ) -> None:
        self._ledger = ledger
        self._requester = requester

    def bind_requester(self, requester: ConfirmationRequester) -> None:
        self._requester = requester

    async def _ask(self, cand: Commitment) -> str | None:
        if self._requester is None:
            return None
        try:
            got = await self._requester.request_commitment_confirm(
                cand.id, preview=await self._ledger.card_preview(cand)
            )
        except Exception as exc:  # noqa: BLE001 - stays in the review queue
            log.warning("commitment confirm request failed err=%s", type(exc).__name__)
            return "failed"
        outcome = str(getattr(got, "value", got))
        if outcome not in ("requested", "duplicate"):
            log.info("commitment card not sent outcome=%s", outcome[:24])  # queued
        return outcome

    async def on_extracted(
        self, event_id: uuid.UUID, items: Sequence[tuple[str, float, str | None]]
    ) -> list[Commitment]:
        made: list[Commitment] = []
        for description, confidence, phrase in items:
            cand = await self._ledger.propose(
                event_id, description=description, confidence=confidence, due_phrase=phrase
            )
            if cand is not None:
                made.append(cand)
                await self._ask(cand)
                continue
            # A restatement must not vanish silently: if the earlier candidate was never
            # decided (card expired or unsent) ask again. An open card answers "duplicate".
            dup = await self._ledger.duplicate_of(description)
            if dup is not None and dup.status == "candidate" and dup.source_event_id != event_id:
                await self._ask(dup)
        return made

    async def drain(self, *, limit: int = 5) -> int:
        """Ask for queued candidates that have no open card, oldest first, until the
        approval service says stop. Returns the number of cards sent."""
        sent = 0
        for cand in await self._ledger.review_queue(limit=limit):
            outcome = await self._ask(cand)
            if outcome == "requested":
                sent += 1
            elif outcome != "duplicate":
                break  # too_many / failed: try again at the next card close
        return sent

    async def card_closed(self, candidate_id: uuid.UUID, outcome: str) -> None:
        """A commitment card ended (`rejected` by the user's tap, `expired`, `done`).
        A Reject is recorded in the ledger so the candidate leaves the review queue;
        then the next queued candidate gets its turn."""
        if outcome == "rejected":
            try:
                await self._ledger.reject_via_card(candidate_id)
            except LedgerRefused as exc:
                log.info("reject not recorded code=%s", exc.code)
        await self.drain()
