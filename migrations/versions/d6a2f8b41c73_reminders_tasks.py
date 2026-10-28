# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""reminder + task tables (P3.3).

* `reminder`, `task`: persisted working state for the internal reminders and
  tasks tools (previously process-local dicts + APScheduler). A reminder's
  delivery is a row in the existing `job` table, written in the same
  transaction by the application.

Downgrade DROPS the reminder and task tables (their rows are destroyed).

Revision ID: d6a2f8b41c73
Revises: c4e8a1d92b37
Create Date: 2026-10-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6a2f8b41c73"  # pragma: allowlist secret
down_revision: str | None = "c4e8a1d92b37"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TZ = sa.DateTime(timezone=True)
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "reminder",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("remind_at", TZ, nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("fired_at", TZ, nullable=True),
        sa.Column("source_event_id", UUID, sa.ForeignKey("event.id"), nullable=True),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        sa.Column("updated_at", TZ, nullable=False, server_default=NOW),
        sa.CheckConstraint("status IN ('pending', 'fired')", name="ck_reminder_status"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 500", name="ck_reminder_title"),
    )
    op.create_index("idx_reminder_status_at", "reminder", ["status", "remind_at"])
    # Idempotent creation (M5): a replayed turn cannot make a second reminder.
    op.create_index(
        "uq_reminder_source_title_at",
        "reminder",
        ["source_event_id", "title", "remind_at"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )

    op.create_table(
        "task",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("done_at", TZ, nullable=True),
        sa.Column("source_event_id", UUID, sa.ForeignKey("event.id"), nullable=True),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        sa.Column("updated_at", TZ, nullable=False, server_default=NOW),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 500", name="ck_task_title"),
        sa.CheckConstraint("(done_at IS NOT NULL) = done", name="ck_task_done_at"),
    )
    op.create_index("idx_task_done_created", "task", ["done", "created_at"])
    op.create_index(
        "uq_task_source_title",
        "task",
        ["source_event_id", "title"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("task")
    op.drop_table("reminder")
