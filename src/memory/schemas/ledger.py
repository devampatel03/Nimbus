# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Commitment ledger rows (P3.9, ADR-0022): append-only, bi-temporal by version.

One logical commitment is a chain of immutable rows sharing `commitment_key`:
`candidate` -> `confirmed` -> `completed` | `cancelled` (or `candidate` ->
`rejected`). A status change is a NEW row (`version + 1`, `supersedes_id` the
previous row); no row is ever updated or deleted (`nimbus_append_only('','')`).

The DB itself refuses the dangerous inserts (`nimbus_ledger_guard`, an INSERT
trigger): a `confirmed` row needs a `pending_action` that is consumed, in time,
`commitment`/`confirm`, for exactly the candidate it supersedes. So a bug in
code, or text in a message, cannot confirm anything (INV-5, R8).

`LEDGER_GUARD_FN_SQL` is the ORM-side copy; the Alembic revision carries a
frozen copy of the same SQL.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base
from src.memory.schemas.provenance import confidence_check, lane_check, promotion_check
from src.memory.schemas.triggers import pin_search_path_sql

_TZ = DateTime(timezone=True)

STATUSES = ("candidate", "confirmed", "rejected", "completed", "cancelled")
DUE_STATES = ("none", "resolved", "unresolved")

LEDGER_GUARD_FN_SQL = """
CREATE OR REPLACE FUNCTION nimbus_ledger_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    prev commitment_ledger%ROWTYPE;
    pa   pending_action%ROWTYPE;
BEGIN
    IF NEW.version = 1 THEN
        IF NEW.status <> 'candidate' OR NEW.supersedes_id IS NOT NULL THEN
            RAISE EXCEPTION 'commitment_ledger: a chain starts with a candidate (INV-5)'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO prev FROM commitment_ledger WHERE id = NEW.supersedes_id;
    IF NOT FOUND OR prev.commitment_key <> NEW.commitment_key
       OR prev.version + 1 <> NEW.version THEN
        RAISE EXCEPTION 'commitment_ledger: supersedes the wrong row'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.description <> prev.description OR NEW.lane <> prev.lane THEN
        RAISE EXCEPTION 'commitment_ledger: description and lane are fixed for a chain'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT ((prev.status = 'candidate' AND NEW.status IN ('candidate', 'confirmed', 'rejected'))
         OR (prev.status = 'confirmed' AND NEW.status IN ('completed', 'cancelled'))) THEN
        RAISE EXCEPTION 'commitment_ledger: illegal transition % -> %', prev.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status <> 'candidate' AND NEW.due_at IS DISTINCT FROM prev.due_at THEN
        RAISE EXCEPTION 'commitment_ledger: only a candidate may change its due time'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'confirmed' THEN
        SELECT * INTO pa FROM pending_action WHERE id = NEW.promoted_approval_id;
        IF NOT FOUND OR pa.status <> 'consumed' OR pa.consumed_at IS NULL
           OR pa.consumed_at > pa.expires_at
           OR pa.tool <> 'commitment' OR pa.action <> 'confirm'
           OR pa.payload ->> 'candidate_id' IS DISTINCT FROM NEW.supersedes_id::text THEN
            RAISE EXCEPTION 'commitment_ledger: no consumed approval for this candidate (INV-5, R8)'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$
"""

LEDGER_GUARD_TRIGGER_SQL = (
    "CREATE TRIGGER trg_commitment_ledger_guard BEFORE INSERT ON commitment_ledger "
    "FOR EACH ROW EXECUTE FUNCTION nimbus_ledger_guard()"
)


class CommitmentLedger(Base):
    __tablename__ = "commitment_ledger"
    __table_args__ = (
        lane_check("commitment_ledger"),
        confidence_check("commitment_ledger"),
        promotion_check("commitment_ledger"),
        CheckConstraint(
            "status IN ('candidate', 'confirmed', 'rejected', 'completed', 'cancelled')",
            name="ck_commitment_ledger_status",
        ),
        CheckConstraint(
            "due_state IN ('none', 'resolved', 'unresolved')",
            name="ck_commitment_ledger_due_state",
        ),
        CheckConstraint(
            "(due_state = 'resolved') = (due_at IS NOT NULL)", name="ck_commitment_ledger_due"
        ),
        CheckConstraint("version >= 1", name="ck_commitment_ledger_version"),
        CheckConstraint(
            "(version = 1) = (supersedes_id IS NULL)", name="ck_commitment_ledger_chain"
        ),
        CheckConstraint(
            "char_length(description) BETWEEN 1 AND 500", name="ck_commitment_ledger_desc"
        ),
        # R8/INV-5: only a row stamped with an approval is `confirmed`, and a
        # candidate is never promoted.
        CheckConstraint(
            "(status = 'confirmed') = (promoted_approval_id IS NOT NULL)",
            name="ck_commitment_ledger_confirm_needs_approval",
        ),
        CheckConstraint(
            "status <> 'candidate' OR promoted = false", name="ck_commitment_ledger_candidate"
        ),
        Index("uq_commitment_ledger_chain", "commitment_key", "version", unique=True),
        Index(
            "uq_commitment_ledger_approval",
            "promoted_approval_id",
            unique=True,
            postgresql_where=text("promoted_approval_id IS NOT NULL"),
        ),
        Index("idx_commitment_ledger_status_due", "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    commitment_key: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("commitment_ledger.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    due_state: Mapped[str] = mapped_column(nullable=False, server_default=text("'none'"))
    due_phrase: Mapped[str | None] = mapped_column(nullable=True)
    norm_key: Mapped[str] = mapped_column(nullable=False)
    # Idempotency: unique across the table (replayed event / replayed transition).
    dedupe_key: Mapped[str] = mapped_column(nullable=False, unique=True)
    # R7 provenance: every column non-null; lane has NO server default (R8).
    source_event_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=False
    )
    lane: Mapped[str] = mapped_column(nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    promoted: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    promoted_approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pending_action.id"), nullable=True
    )


def _fn_ddl(sql: str) -> DDL:
    return DDL(sql.replace("%", "%%")).execute_if(  # type: ignore[no-untyped-call]
        dialect="postgresql"
    )


# `%ROWTYPE` needs the table to exist, so the function is created after it.
event.listen(CommitmentLedger.__table__, "after_create", _fn_ddl(LEDGER_GUARD_FN_SQL))
event.listen(
    CommitmentLedger.__table__,
    "after_create",
    _fn_ddl(pin_search_path_sql("nimbus_ledger_guard()")),
)
event.listen(CommitmentLedger.__table__, "after_create", _fn_ddl(LEDGER_GUARD_TRIGGER_SQL))
