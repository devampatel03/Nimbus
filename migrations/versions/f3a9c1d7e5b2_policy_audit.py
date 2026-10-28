# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""policy_audit table (P3.2, INV-5; P3.1 review LOW-6).

Append-only record of every policy decision and every approval lifecycle event
(requested, rejected, step-up, consumed, executed, forged/replayed callbacks).
Names, rule ids and fixed reason codes only, never argument values or secrets.
Uses the existing `nimbus_append_only()` and `nimbus_no_truncate()` functions
(revision b7d2e9f40a15). Downgrade DROPS the table (audit rows are destroyed).

Revision ID: f3a9c1d7e5b2
Revises: d6a2f8b41c73
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a9c1d7e5b2"  # pragma: allowlist secret
down_revision: str | None = "d6a2f8b41c73"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_audit",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("tool", sa.String(64), nullable=True),
        sa.Column("rule_id", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("tainted", sa.Boolean(), nullable=True),
        sa.Column("step_up", sa.Boolean(), nullable=True),
        sa.Column("executed", sa.Boolean(), nullable=True),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("totp_step", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("kind IN ('decision', 'approval')", name="ck_policy_audit_kind"),
    )
    op.create_index("idx_policy_audit_event_created", "policy_audit", ["event", "created_at"])
    op.execute(
        "CREATE TRIGGER trg_policy_audit_no_truncate BEFORE TRUNCATE ON policy_audit "
        "FOR EACH STATEMENT EXECUTE FUNCTION nimbus_no_truncate()"
    )
    op.execute(
        "CREATE TRIGGER trg_policy_audit_append_only BEFORE UPDATE OR DELETE ON policy_audit "
        "FOR EACH ROW EXECUTE FUNCTION nimbus_append_only('','')"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_policy_audit_append_only ON policy_audit")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_audit_no_truncate ON policy_audit")
    op.drop_index("idx_policy_audit_event_created", table_name="policy_audit")
    op.drop_table("policy_audit")
