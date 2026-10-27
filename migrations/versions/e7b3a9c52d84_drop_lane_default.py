# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""drop the `lane` server default on graph_edge / commitment / decision (P3.3).

The legacy `DEFAULT 'trusted'` (added in P1.2c so unchanged writers stayed
valid) means a writer that forgets the lane silently writes a TRUSTED row
(INV-6 / R8). With the default gone, `lane` is NOT NULL with no default: a
writer that omits it fails loudly. Existing rows keep the lane they already
have; no data is changed. Raw-SQL writers that omit `lane` (some tests) now
fail by design.

Downgrade restores `DEFAULT 'trusted'` (the old, unsafe behaviour); it exists
only so the chain round-trips.

Revision ID: e7b3a9c52d84
Revises: d6a2f8b41c73
Create Date: 2026-10-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7b3a9c52d84"  # pragma: allowlist secret
down_revision: str | None = "d6a2f8b41c73"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LANE_TABLES = ("graph_edge", "commitment", "decision")


def upgrade() -> None:
    for table in LANE_TABLES:
        op.alter_column(table, "lane", server_default=None)


def downgrade() -> None:
    for table in LANE_TABLES:
        op.alter_column(table, "lane", server_default=sa.text("'trusted'"))
