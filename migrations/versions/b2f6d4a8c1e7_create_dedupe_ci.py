# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""case-insensitive create dedupe for reminder and task (P3.3-enable, M5; INV-5).

Before `tasks_create` / `reminders_create` can be switched on
(`POLICY_ENABLE_CREATES`), a replayed turn must not duplicate a row even when the
re-asked model changes the title's case. The per-event unique indexes therefore
compare `lower(title)` instead of `title` (titles are already
whitespace-normalised by `clean_title`):

* reminder: `uq_reminder_source_title_at` -> `uq_reminder_source_ltitle_at`
  on `(source_event_id, lower(title), remind_at) WHERE source_event_id IS NOT NULL`
* task: `uq_task_source_title` -> `uq_task_source_ltitle`
  on `(source_event_id, lower(title)) WHERE source_event_id IS NOT NULL`

Upgrade never deletes data: if existing rows already collide case-insensitively
it raises and the transaction rolls back (resolve by hand, then re-run).
Downgrade restores the case-sensitive indexes; that always succeeds, because
case-insensitive uniqueness implies case-sensitive uniqueness.

Revision ID: b2f6d4a8c1e7
Revises: e5b1c7a3d902
Create Date: 2026-11-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f6d4a8c1e7"  # pragma: allowlist secret
down_revision: str | None = "e5b1c7a3d902"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WHERE = sa.text("source_event_id IS NOT NULL")
_CHECKS = {
    "reminder": "source_event_id, lower(title), remind_at",
    "task": "source_event_id, lower(title)",
}


def _refuse_existing_duplicates() -> None:
    bind = op.get_bind()
    for table, cols in _CHECKS.items():
        n = bind.execute(
            sa.text(
                f"SELECT count(*) FROM (SELECT 1 FROM {table} WHERE source_event_id IS NOT NULL "
                f"GROUP BY {cols} HAVING count(*) > 1) d"
            )
        ).scalar_one()
        if n:
            raise RuntimeError(
                f"{table}: {n} case-variant duplicate group(s) per source event; "
                "resolve them by hand before upgrading (no data is deleted)"
            )


def upgrade() -> None:
    _refuse_existing_duplicates()
    op.drop_index("uq_reminder_source_title_at", table_name="reminder")
    op.create_index(
        "uq_reminder_source_ltitle_at",
        "reminder",
        ["source_event_id", sa.text("lower(title)"), "remind_at"],
        unique=True,
        postgresql_where=_WHERE,
    )
    op.drop_index("uq_task_source_title", table_name="task")
    op.create_index(
        "uq_task_source_ltitle",
        "task",
        ["source_event_id", sa.text("lower(title)")],
        unique=True,
        postgresql_where=_WHERE,
    )


def downgrade() -> None:
    op.drop_index("uq_task_source_ltitle", table_name="task")
    op.create_index(
        "uq_task_source_title",
        "task",
        ["source_event_id", "title"],
        unique=True,
        postgresql_where=_WHERE,
    )
    op.drop_index("uq_reminder_source_ltitle_at", table_name="reminder")
    op.create_index(
        "uq_reminder_source_title_at",
        "reminder",
        ["source_event_id", "title", "remind_at"],
        unique=True,
        postgresql_where=_WHERE,
    )
