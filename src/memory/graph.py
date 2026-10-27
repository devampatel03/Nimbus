# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Graph repository: insert, supersede, and contradiction-detection queries
over `graph_edge` (docs/TECHNICAL_ARCHITECTURE.md sections 6.1, 6.2).

INV-3: this module must never assign to `GraphEdge.object_id`,
`.object_literal` or `.valid_from` after construction, and must never issue
a `sqlalchemy.update(GraphEdge)` touching those columns. Supersession sets
only `valid_to`/`superseded_by` on the old row and inserts a new one.
Statically enforced by tests/eval/invariants/test_inv3_graph_facts_immutable.py.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.memory.schemas.graph import GraphEdge

_INFINITY: ColumnElement[datetime] = literal_column("'infinity'::timestamptz")


class GraphRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_fact(
        self,
        *,
        subject_id: uuid.UUID,
        predicate: str,
        object_id: uuid.UUID | None,
        object_literal: str | None,
        valid_from: datetime,
        source_session: uuid.UUID,
        confidence: float,
        lane: Literal["trusted", "untrusted"],
        source_speaker: uuid.UUID | None = None,
    ) -> GraphEdge:
        """`lane` is required (no default, P3.3): an untrusted edge is born
        unpromoted (R8) and only a consumed approval can promote it."""
        edge = GraphEdge(
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            object_literal=object_literal,
            valid_from=valid_from,
            ingested_at=datetime.now(tz=valid_from.tzinfo),
            source_session=source_session,
            source_speaker=source_speaker,
            confidence=confidence,
            lane=lane,
            promoted=lane == "trusted",
        )
        self._session.add(edge)
        await self._session.flush()
        return edge

    async def supersede(
        self,
        old_edge: GraphEdge,
        *,
        object_id: uuid.UUID | None,
        object_literal: str | None,
        valid_from: datetime,
        confidence: float,
        lane: Literal["trusted", "untrusted"],
    ) -> GraphEdge:
        """Insert a new fact and mark `old_edge` superseded (INV-3): never
        mutates `old_edge.object_id`, `.object_literal` or `.valid_from`.
        """
        new_edge = await self.insert_fact(
            subject_id=old_edge.subject_id,
            predicate=old_edge.predicate,
            object_id=object_id,
            object_literal=object_literal,
            valid_from=valid_from,
            source_session=old_edge.source_session,
            confidence=confidence,
            lane=lane,
            source_speaker=old_edge.source_speaker,
        )
        # Valid time closes when the successor becomes true (bi-temporal as_of
        # reads depend on this), never before the old edge began.
        old_edge.valid_to = max(valid_from, old_edge.valid_from)
        old_edge.superseded_by = new_edge.id
        await self._session.flush()
        return new_edge

    async def find_contradictions(
        self, subject_id: uuid.UUID, predicate: str
    ) -> list[tuple[GraphEdge, GraphEdge]]:
        """MEM-3: same subject+predicate, different object, overlapping
        valid-time — a plain SQL predicate, no LLM involved.
        """
        a = aliased(GraphEdge)
        b = aliased(GraphEdge)
        stmt = (
            select(a, b)
            .join(
                b,
                and_(
                    a.subject_id == b.subject_id,
                    a.predicate == b.predicate,
                    a.id < b.id,
                    or_(
                        a.object_id.is_distinct_from(b.object_id),
                        a.object_literal.is_distinct_from(b.object_literal),
                    ),
                    a.valid_from <= func.coalesce(b.valid_to, _INFINITY),
                    b.valid_from <= func.coalesce(a.valid_to, _INFINITY),
                ),
            )
            .where(a.subject_id == subject_id, a.predicate == predicate)
        )
        result = await self._session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]
