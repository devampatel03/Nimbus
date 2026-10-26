# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""single-use promotion approvals (P2.4 round 1, R8).

One `promoted_approval_id` may stamp at most one memory_chunk and at most one
graph_edge (partial UNIQUE indexes), so a consumed approval cannot be replayed
on a second item even by a code path that skips the store's checks. Fails on a
populated database that already holds duplicate non-null approval ids (none can
exist legitimately). Downgrade drops the indexes only.

Revision ID: c4e8a1d92b37
Revises: b7d2e9f40a15
Create Date: 2026-10-26

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e8a1d92b37"  # pragma: allowlist secret
down_revision: str | None = "b7d2e9f40a15"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("memory_chunk", "graph_edge"):
        op.create_index(
            f"uq_{table}_promoted_approval",
            table,
            ["promoted_approval_id"],
            unique=True,
            postgresql_where="promoted_approval_id IS NOT NULL",
        )


def downgrade() -> None:
    for table in ("graph_edge", "memory_chunk"):
        op.drop_index(f"uq_{table}_promoted_approval", table_name=table)
