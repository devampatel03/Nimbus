# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""personality tables: agent_stance, user_trait, trait_evidence.

Matches the ORM in src/personality/{agent_character,user_profile}.py.
Downgrade drops the three tables (destroys stance/trait data; irreversible
with respect to row contents).

Revision ID: a1c3f5e7b9d2
Revises: 0dc85954088a
Create Date: 2026-10-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c3f5e7b9d2"  # pragma: allowlist secret
down_revision: str | None = "0dc85954088a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_stance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("position", sa.String(), nullable=False),
        sa.Column("reasoning", sa.String(), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strength", sa.String(), nullable=False),
        sa.Column(
            "revised_from",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_stance.id"),
            nullable=True,
        ),
        sa.Column("revision_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            "strength IN ('tentative', 'held', 'firm')",
            name="ck_agent_stance_strength",
        ),
    )

    op.create_table(
        "user_trait",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("statement", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("first_observed", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reinforced", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_status", sa.String(), nullable=False),
        sa.Column(
            "superseded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_trait.id"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "user_status IN ('unreviewed', 'approved', 'edited', 'vetoed')",
            name="ck_user_trait_status",
        ),
    )

    op.create_table(
        "trait_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trait_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_trait.id"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("excerpt", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("trait_evidence")
    op.drop_table("user_trait")
    op.drop_table("agent_stance")
