# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""commitment_ledger table (P3.9, ADR-0022; INV-3, INV-5, R7, R8).

Append-only chain of immutable rows per commitment (candidate -> confirmed ->
completed | cancelled, or candidate -> rejected). A `confirmed` row is accepted
by the DB only with a consumed, in-time `commitment`/`confirm` approval for the
exact candidate it supersedes (`nimbus_ledger_guard`, frozen copy of
src/memory/schemas/ledger.py). Uses `nimbus_append_only()` / `nimbus_no_truncate()`
(revision b7d2e9f40a15). Downgrade DROPS the table (ledger rows are destroyed).

Revision ID: a9d4c6e2f801
Revises: f3a9c1d7e5b2
Create Date: 2026-10-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9d4c6e2f801"  # pragma: allowlist secret
down_revision: str | None = "f3a9c1d7e5b2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TZ = sa.DateTime(timezone=True)

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


def upgrade() -> None:
    op.create_table(
        "commitment_ledger",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("commitment_key", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", UUID, sa.ForeignKey("commitment_ledger.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("due_at", TZ, nullable=True),
        sa.Column("due_state", sa.String(), nullable=False, server_default=sa.text("'none'")),
        sa.Column("due_phrase", sa.String(), nullable=True),
        sa.Column("norm_key", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False, unique=True),
        sa.Column("source_event_id", UUID, sa.ForeignKey("event.id"), nullable=False),
        sa.Column("lane", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("promoted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("promoted_approval_id", UUID, sa.ForeignKey("pending_action.id"), nullable=True),
        sa.CheckConstraint("lane IN ('trusted', 'untrusted')", name="ck_commitment_ledger_lane"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_commitment_ledger_confidence"),
        sa.CheckConstraint(
            "lane = 'trusted' OR promoted = false OR promoted_approval_id IS NOT NULL",
            name="ck_commitment_ledger_untrusted_promotion",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'confirmed', 'rejected', 'completed', 'cancelled')",
            name="ck_commitment_ledger_status",
        ),
        sa.CheckConstraint(
            "due_state IN ('none', 'resolved', 'unresolved')",
            name="ck_commitment_ledger_due_state",
        ),
        sa.CheckConstraint(
            "(due_state = 'resolved') = (due_at IS NOT NULL)", name="ck_commitment_ledger_due"
        ),
        sa.CheckConstraint("version >= 1", name="ck_commitment_ledger_version"),
        sa.CheckConstraint(
            "(version = 1) = (supersedes_id IS NULL)", name="ck_commitment_ledger_chain"
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 500", name="ck_commitment_ledger_desc"
        ),
        sa.CheckConstraint(
            "(status = 'confirmed') = (promoted_approval_id IS NOT NULL)",
            name="ck_commitment_ledger_confirm_needs_approval",
        ),
        sa.CheckConstraint(
            "status <> 'candidate' OR promoted = false", name="ck_commitment_ledger_candidate"
        ),
    )
    op.create_index(
        "uq_commitment_ledger_chain",
        "commitment_ledger",
        ["commitment_key", "version"],
        unique=True,
    )
    op.create_index(
        "uq_commitment_ledger_approval",
        "commitment_ledger",
        ["promoted_approval_id"],
        unique=True,
        postgresql_where=sa.text("promoted_approval_id IS NOT NULL"),
    )
    op.create_index("idx_commitment_ledger_status_due", "commitment_ledger", ["status", "due_at"])
    op.execute(LEDGER_GUARD_FN_SQL)  # %ROWTYPE needs the table to exist
    op.execute(LEDGER_GUARD_TRIGGER_SQL)
    op.execute(
        "CREATE TRIGGER trg_commitment_ledger_append_only BEFORE UPDATE OR DELETE "
        "ON commitment_ledger FOR EACH ROW EXECUTE FUNCTION nimbus_append_only('','')"
    )
    op.execute(
        "CREATE TRIGGER trg_commitment_ledger_no_truncate BEFORE TRUNCATE ON commitment_ledger "
        "FOR EACH STATEMENT EXECUTE FUNCTION nimbus_no_truncate()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_ledger_no_truncate ON commitment_ledger")
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_ledger_append_only ON commitment_ledger")
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_ledger_guard ON commitment_ledger")
    op.drop_table("commitment_ledger")
    op.execute("DROP FUNCTION IF EXISTS nimbus_ledger_guard()")
