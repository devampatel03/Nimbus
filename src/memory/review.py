# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Untrusted-memory review queue (P4.6; R8, R7, INV-3, INV-5, INV-6).

Untrusted-origin rows (chunks and graph edges with `lane='untrusted'`) are born
`promoted=false` and stay out of trusted retrieval. This module is the ONLY path
that surfaces them for a decision, and it decides nothing itself:

* `list_pending` is a read: untrusted, unpromoted, not invalidated items,
  oldest first, previews clipped and secret-scrubbed (the card labels them
  untrusted).
* `request_review` / `drain` ask the user through
  `ApprovalService.request_memory_promotion` (one card per item, bounded per call;
  there is no bulk approve). Nothing is sent unless the caller (weekly review,
  P5.1) calls them; no proactive messaging here.
* `promote` is the approval executor's promoter: it runs the single-use binding
  (consumed, in-time `memory`/`promote` approval naming exactly this item). The DB
  trigger `nimbus_promotion_guard` re-checks it for chunks and edges.
* `reject_via_card` runs when the user taps Reject: the item is invalidated
  (record-time retraction, never a delete). It only touches untrusted, unpromoted
  rows, so it can never retract a trusted fact.

No model output reaches any of these methods with authority: item ids come from the
database and approvals only from a genuine user tap.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.llm.scrub import redact_secrets
from src.memory.store import (
    PROMOTE_CHUNK_SQL,
    PROMOTE_EDGE_SQL,
    ApprovalRequiredError,
    ChunkNotFoundError,
)

log = logging.getLogger(__name__)

MAX_PREVIEW = 200
DEFAULT_CARDS_PER_DRAIN = 5

ItemKind = Literal["chunk", "edge"]

_PENDING_SQL = """
SELECT kind, id, preview, confidence, source_event_id, created_at FROM (
    SELECT 'chunk' AS kind, c.id, c.content AS preview, c.confidence,
           c.source_event_id, c.created_at
    FROM memory_chunk c
    WHERE c.lane = 'untrusted' AND c.promoted = false AND c.invalidated_at IS NULL
    UNION ALL
    SELECT 'edge', e.id,
           s.canonical_name || ' ' || e.predicate || ' '
               || coalesce(e.object_literal, o.canonical_name, ''),
           e.confidence, e.source_event_id, e.ingested_at
    FROM graph_edge e
    JOIN graph_node s ON s.id = e.subject_id
    LEFT JOIN graph_node o ON o.id = e.object_id
    WHERE e.lane = 'untrusted' AND e.promoted = false AND e.invalidated_at IS NULL
) q
ORDER BY created_at, id
LIMIT :n OFFSET :o
"""

# Fixed text; only the table name differs, and both come from this module.
_REJECT_TEMPLATE = """
UPDATE {table} SET invalidated_at = clock_timestamp()
WHERE id = :i AND lane = 'untrusted' AND promoted = false AND invalidated_at IS NULL
RETURNING id
"""
_REJECT_SQL: dict[ItemKind, str] = {
    "chunk": _REJECT_TEMPLATE.format(table="memory_chunk"),
    "edge": _REJECT_TEMPLATE.format(table="graph_edge"),
}
_PROMOTE_SQL: dict[ItemKind, str] = {"chunk": PROMOTE_CHUNK_SQL, "edge": PROMOTE_EDGE_SQL}
_KINDS: tuple[ItemKind, ...] = ("chunk", "edge")


@dataclass(frozen=True)
class PendingItem:
    kind: ItemKind
    item_id: uuid.UUID
    preview: str  # untrusted text: clipped, scrubbed; render only as DATA
    confidence: float
    source_event_id: uuid.UUID | None
    created_at: datetime


class PromotionRequester(Protocol):
    async def request_memory_promotion(
        self, item_id: uuid.UUID, *, preview: str | None = None
    ) -> Any: ...


def _clean(raw: str) -> str:
    folded = unicodedata.normalize("NFKC", raw)
    folded = "".join(ch if ch.isprintable() else " " for ch in folded)
    return " ".join(redact_secrets(folded).split())[:MAX_PREVIEW]


class ReviewQueue:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        requester: PromotionRequester | None = None,
    ) -> None:
        self._sf = sessions
        self._requester = requester

    # -- reads --------------------------------------------------------------

    async def list_pending(self, *, limit: int = 50, offset: int = 0) -> list[PendingItem]:
        async with self._sf() as s:
            rows = (
                await s.execute(
                    text(_PENDING_SQL), {"n": max(1, min(limit, 200)), "o": max(0, offset)}
                )
            ).all()
        return [
            PendingItem(
                kind=r.kind,
                item_id=r.id,
                preview=_clean(str(r.preview)),
                confidence=float(r.confidence),
                source_event_id=r.source_event_id,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def pending_count(self) -> int:
        async with self._sf() as s:
            n: int = (
                await s.execute(
                    text(
                        "SELECT (SELECT count(*) FROM memory_chunk WHERE lane = 'untrusted' "
                        "AND promoted = false AND invalidated_at IS NULL) + "
                        "(SELECT count(*) FROM graph_edge WHERE lane = 'untrusted' "
                        "AND promoted = false AND invalidated_at IS NULL)"
                    )
                )
            ).scalar_one()
        return int(n)

    # -- ask the user ---------------------------------------------------------

    async def request_review(self, item: PendingItem) -> Any:
        """One approval card for this one item. Returns the service's `AskOutcome`."""
        if self._requester is None:
            raise RuntimeError("review queue has no approval service")
        return await self._requester.request_memory_promotion(item.item_id, preview=item.preview)

    async def drain(self, *, max_cards: int = DEFAULT_CARDS_PER_DRAIN) -> int:
        """Ask about the oldest pending items, at most `max_cards` cards, stopping at
        the first refusal (open-card cap, send failure). Never approves anything."""
        sent = 0
        for item in await self.list_pending(limit=max_cards * 4):
            if sent >= max_cards:
                break
            outcome = str(getattr(await self.request_review(item), "value", ""))
            if outcome == "requested":
                sent += 1
            elif outcome != "duplicate":
                break
        return sent

    # -- decisions (called by the approval service only) -----------------------

    async def promote(self, item_id: uuid.UUID, approval_id: uuid.UUID) -> None:
        """R8 promoter for `ApprovalService.bind_promoter`. Runs the single-use
        binding against whichever table holds `item_id`."""
        for kind in _KINDS:
            try:
                async with self._sf() as s, s.begin():
                    done = (
                        await s.execute(text(_PROMOTE_SQL[kind]), {"i": item_id, "a": approval_id})
                    ).first()
            except IntegrityError:  # approval already stamped on another row, or DB guard
                raise ApprovalRequiredError("approval_already_used") from None
            if done is not None:
                return
        if not await self._known(item_id):
            raise ChunkNotFoundError("item_not_found")
        raise ApprovalRequiredError("promotion_refused")

    async def reject_via_card(self, item_id: uuid.UUID) -> bool:
        """The user tapped Reject: retract the item. True when a row was retracted."""
        for kind in _KINDS:
            async with self._sf() as s, s.begin():
                done = (await s.execute(text(_REJECT_SQL[kind]), {"i": item_id})).first()
            if done is not None:
                return True
        return False

    async def card_closed(self, item_id: uuid.UUID, outcome: str) -> None:
        """Card ended: `rejected` retracts the item; `expired` leaves it queued."""
        if outcome == "rejected" and await self.reject_via_card(item_id):
            log.info("untrusted memory item rejected")

    async def _known(self, item_id: uuid.UUID) -> bool:
        async with self._sf() as s:
            n: int = (
                await s.execute(
                    text(
                        "SELECT (SELECT count(*) FROM memory_chunk WHERE id = :i) + "
                        "(SELECT count(*) FROM graph_edge WHERE id = :i)"
                    ),
                    {"i": item_id},
                )
            ).scalar_one()
        return int(n) > 0
