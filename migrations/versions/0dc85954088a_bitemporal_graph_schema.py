# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""bi-temporal graph schema (T2.9): graph_node, graph_edge, session,
commitment, decision, person.

Revision ID: 0dc85954088a
Revises:
Create Date: 2026-09-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0dc85954088a"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_node",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "session",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_id", sa.String(), nullable=False),
    )

    op.create_table(
        "person",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "graph_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_node.id"),
            nullable=True,
        ),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("voice_print", sa.LargeBinary(), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "graph_edge",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_node.id"),
            nullable=False,
        ),
        sa.Column("predicate", sa.String(), nullable=False),
        sa.Column(
            "object_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_node.id"),
            nullable=True,
        ),
        sa.Column("object_literal", sa.String(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source_session",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session.id"),
            nullable=False,
        ),
        sa.Column(
            "source_speaker",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person.id"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "superseded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_edge.id"),
            nullable=True,
        ),
        sa.Column("user_verified", sa.Boolean(), nullable=True),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_graph_edge_confidence"),
        sa.CheckConstraint(
            "object_id IS NOT NULL OR object_literal IS NOT NULL",
            name="ck_graph_edge_object_present",
        ),
    )
    op.create_index(
        "idx_edge_current",
        "graph_edge",
        ["subject_id", "predicate"],
        postgresql_where=sa.text("valid_to IS NULL AND invalidated_at IS NULL"),
    )
    op.create_index("idx_edge_temporal", "graph_edge", ["subject_id", "valid_from", "valid_to"])

    op.create_table(
        "commitment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_session",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session.id"),
            nullable=False,
        ),
        sa.Column(
            "owner_person_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person.id"),
            nullable=True,
        ),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "closure_source_session",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session.id"),
            nullable=True,
        ),
        sa.CheckConstraint("status IN ('open', 'closed', 'broken')", name="ck_commitment_status"),
    )

    op.create_table(
        "decision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_session",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session.id"),
            nullable=False,
        ),
        sa.Column("decision_text", sa.String(), nullable=False),
        sa.Column("reasoning", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("outcome_captured_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("decision")
    op.drop_table("commitment")
    op.drop_index("idx_edge_temporal", table_name="graph_edge")
    op.drop_index("idx_edge_current", table_name="graph_edge")
    op.drop_table("graph_edge")
    op.drop_table("person")
    op.drop_table("session")
    op.drop_table("graph_node")
