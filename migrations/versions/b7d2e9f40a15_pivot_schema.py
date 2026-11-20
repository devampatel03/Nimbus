# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""pivot schema (P1.2c): event, memory_chunk, job, inbound_update,
pending_action, llm_audit, system_flag; provenance columns on graph_edge /
commitment / decision; append-only triggers (INV-3 / R7).

The trigger SQL below is a frozen copy of src/memory/schemas/triggers.py.
Downgrade drops the new tables and columns (destroys their rows).

Revision ID: b7d2e9f40a15
Revises: a1c3f5e7b9d2
Create Date: 2026-10-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d2e9f40a15"  # pragma: allowlist secret
down_revision: str | None = "a1c3f5e7b9d2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TZ = sa.DateTime(timezone=True)
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")

LANE_SQL = "lane IN ('trusted', 'untrusted')"
CONF_SQL = "confidence BETWEEN 0 AND 1"
PROMO_SQL = "lane = 'trusted' OR promoted = false OR promoted_approval_id IS NOT NULL"

APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION nimbus_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    mutable text[] := string_to_array(coalesce(TG_ARGV[0], ''), ',');
    once    text[] := string_to_array(coalesce(TG_ARGV[1], ''), ',');
    col     text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'append-only table %: DELETE is forbidden (INV-3)', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (to_jsonb(NEW) - mutable - once) IS DISTINCT FROM (to_jsonb(OLD) - mutable - once) THEN
        RAISE EXCEPTION 'append-only table %: immutable column changed (INV-3)', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    FOREACH col IN ARRAY once LOOP
        IF col <> '' AND (to_jsonb(OLD) -> col) <> 'null'::jsonb
           AND (to_jsonb(NEW) -> col) IS DISTINCT FROM (to_jsonb(OLD) -> col) THEN
            RAISE EXCEPTION 'append-only table %: set-once column % rewritten (INV-3)',
                TG_TABLE_NAME, col
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END
$fn$
"""

SINGLE_USE_FN = """
CREATE OR REPLACE FUNCTION nimbus_single_use_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'pending_action % is already %; single-use', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$
"""

NO_TRUNCATE_FN = """
CREATE OR REPLACE FUNCTION nimbus_no_truncate() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'append-only table %: TRUNCATE is forbidden (INV-3)', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END
$fn$
"""

APPROVAL_FK_TABLES = ("memory_chunk", "graph_edge", "commitment", "decision")

# table -> (mutable columns, set-once columns)
APPEND_ONLY: dict[str, tuple[str, str]] = {
    "event": ("", ""),
    "llm_audit": ("payload_redacted_enc", ""),
    "inbound_update": ("processed_at,event_id", "processed_at,event_id"),
    "memory_chunk": (
        "invalidated_at,superseded_by,promoted,promoted_approval_id",
        "invalidated_at,superseded_by,promoted_approval_id",
    ),
    "graph_edge": (
        "valid_to,invalidated_at,superseded_by,user_verified,promoted,promoted_approval_id",
        "valid_to,invalidated_at,superseded_by,promoted_approval_id",
    ),
    "commitment": (
        "status,closed_at,closure_source_session,promoted,promoted_approval_id",
        "closed_at,closure_source_session,promoted_approval_id",
    ),
    "decision": (
        "outcome,outcome_captured_at,promoted,promoted_approval_id",
        "outcome,outcome_captured_at,promoted_approval_id",
    ),
    "pending_action": ("status,consumed_at", "consumed_at"),
}

LEGACY_PROVENANCE = ("graph_edge", "commitment", "decision")


class Vector1024(sa.types.UserDefinedType):  # type: ignore[type-arg]
    """DDL-only stand-in for pgvector vector(1024) (frozen; no app import)."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "vector(1024)"


def _trigger_name(table: str) -> str:
    return f"trg_{table}_append_only"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "event",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("lane", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("body", sa.String(), nullable=True),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("file_unique_id", sa.String(), nullable=True),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        sa.UniqueConstraint("source_key"),
        sa.CheckConstraint(LANE_SQL, name="ck_event_lane"),
        sa.CheckConstraint(CONF_SQL, name="ck_event_confidence"),
    )

    # Provenance columns on the legacy fact tables. Defaults keep the existing
    # writers (graph.py / manager.py) valid without code changes.
    for table in LEGACY_PROVENANCE:
        op.add_column(table, sa.Column("source_event_id", UUID, nullable=True))
        op.create_foreign_key(
            f"fk_{table}_source_event_id", table, "event", ["source_event_id"], ["id"]
        )
        op.add_column(
            table,
            sa.Column("lane", sa.String(), nullable=False, server_default=sa.text("'trusted'")),
        )
        op.add_column(
            table,
            sa.Column("promoted", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        )
        op.add_column(table, sa.Column("promoted_approval_id", UUID, nullable=True))
        op.create_check_constraint(f"ck_{table}_lane", table, LANE_SQL)
        op.create_check_constraint(f"ck_{table}_untrusted_promotion", table, PROMO_SQL)

    op.add_column(
        "decision",
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
    )
    op.create_check_constraint("ck_decision_confidence", "decision", CONF_SQL)

    op.create_table(
        "memory_chunk",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("source_event_id", UUID, sa.ForeignKey("event.id"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("embedding", Vector1024(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False, server_default=sa.text("1024")),
        sa.Column("lane", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("importance", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("promoted", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("promoted_approval_id", UUID, nullable=True),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        sa.Column("invalidated_at", TZ, nullable=True),
        sa.Column("superseded_by", UUID, sa.ForeignKey("memory_chunk.id"), nullable=True),
        sa.UniqueConstraint("source_event_id", "chunk_index", name="uq_memory_chunk_event_idx"),
        sa.CheckConstraint(LANE_SQL, name="ck_memory_chunk_lane"),
        sa.CheckConstraint(CONF_SQL, name="ck_memory_chunk_confidence"),
        sa.CheckConstraint(PROMO_SQL, name="ck_memory_chunk_untrusted_promotion"),
        sa.CheckConstraint("importance BETWEEN 0 AND 1", name="ck_memory_chunk_importance"),
        sa.CheckConstraint("dim = 1024", name="ck_memory_chunk_dim"),
        sa.CheckConstraint("superseded_by <> id", name="ck_memory_chunk_not_self_superseded"),
    )
    op.create_index("idx_memory_chunk_created", "memory_chunk", ["created_at"])
    op.execute(
        "CREATE INDEX idx_memory_chunk_embedding_hnsw ON memory_chunk "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "job",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("run_at", TZ, nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("rrule", sa.String(), nullable=True),
        sa.Column("proactive", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("locked_at", TZ, nullable=True),
        sa.Column("source_event_id", UUID, sa.ForeignKey("event.id"), nullable=True),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        sa.Column("updated_at", TZ, nullable=False, server_default=NOW),
        sa.UniqueConstraint("dedupe_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed', 'dead', 'cancelled')",
            name="ck_job_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_job_attempts"),
    )
    op.create_index(
        "idx_job_due", "job", ["run_at"], postgresql_where=sa.text("status = 'pending'")
    )

    op.create_table(
        "inbound_update",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("from_user_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("received_at", TZ, nullable=False, server_default=NOW),
        sa.Column("processed_at", TZ, nullable=True),
        sa.Column("event_id", UUID, sa.ForeignKey("event.id"), nullable=True),
        sa.UniqueConstraint("update_id"),
    )

    op.create_table(
        "pending_action",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("requires_totp", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        sa.Column("consumed_at", TZ, nullable=True),
        sa.Column("source_event_id", UUID, sa.ForeignKey("event.id"), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'expired', 'cancelled', 'rejected')",
            name="ck_pending_action_status",
        ),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_pending_action_sha256"),
        sa.CheckConstraint("expires_at > created_at", name="ck_pending_action_expiry"),
        sa.CheckConstraint(
            "(status = 'consumed') = (consumed_at IS NOT NULL)",
            name="ck_pending_action_consumed_at",
        ),
    )
    op.create_index("idx_pending_action_status_expiry", "pending_action", ["status", "expires_at"])

    op.create_table(
        "llm_audit",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("blocked_reason", sa.String(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_inr", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("turn_id", UUID, nullable=True),
        sa.Column("source_event_id", UUID, sa.ForeignKey("event.id"), nullable=True),
        sa.Column("payload_redacted_enc", sa.LargeBinary(), nullable=True),
        sa.Column("payload_expires_at", TZ, nullable=True),
        sa.CheckConstraint("status IN ('ok', 'blocked', 'error')", name="ck_llm_audit_status"),
    )
    op.create_index("idx_llm_audit_created", "llm_audit", ["created_at"])

    op.create_table(
        "system_flag",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", TZ, nullable=False, server_default=NOW),
        sa.Column("updated_by", sa.String(), nullable=True),
    )

    # R8: an approval reference must be a real pending_action row (no fabricated UUIDs).
    for table in APPROVAL_FK_TABLES:
        op.create_foreign_key(
            f"fk_{table}_promoted_approval_id",
            table,
            "pending_action",
            ["promoted_approval_id"],
            ["id"],
        )

    # Append-only triggers (INV-3 / R7). Installed last so the column adds above
    # are not subject to them.
    op.execute(APPEND_ONLY_FN)
    op.execute(SINGLE_USE_FN)
    op.execute(NO_TRUNCATE_FN)
    for table, (mutable, once) in APPEND_ONLY.items():
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION nimbus_no_truncate()"
        )
        op.execute(
            f"CREATE TRIGGER {_trigger_name(table)} BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION nimbus_append_only('{mutable}','{once}')"
        )
    op.execute(
        "CREATE TRIGGER trg_pending_action_single_use BEFORE UPDATE ON pending_action "
        "FOR EACH ROW EXECUTE FUNCTION nimbus_single_use_guard()"
    )


def downgrade() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {_trigger_name(table)} ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_pending_action_single_use ON pending_action")
    op.execute("DROP FUNCTION IF EXISTS nimbus_single_use_guard()")
    op.execute("DROP FUNCTION IF EXISTS nimbus_no_truncate()")
    for table in APPROVAL_FK_TABLES:
        op.drop_constraint(f"fk_{table}_promoted_approval_id", table, type_="foreignkey")
    op.execute("DROP FUNCTION IF EXISTS nimbus_append_only()")

    op.drop_table("system_flag")
    op.drop_table("llm_audit")
    op.drop_table("pending_action")
    op.drop_table("inbound_update")
    op.drop_table("job")
    op.drop_table("memory_chunk")

    op.drop_constraint("ck_decision_confidence", "decision", type_="check")
    op.drop_column("decision", "confidence")
    for table in LEGACY_PROVENANCE:
        op.drop_constraint(f"ck_{table}_untrusted_promotion", table, type_="check")
        op.drop_constraint(f"ck_{table}_lane", table, type_="check")
        op.drop_column(table, "promoted_approval_id")
        op.drop_column(table, "promoted")
        op.drop_column(table, "lane")
        op.drop_constraint(f"fk_{table}_source_event_id", table, type_="foreignkey")
        op.drop_column(table, "source_event_id")

    op.drop_table("event")
    # The vector extension is left installed: it is shared infrastructure.
