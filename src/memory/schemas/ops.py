# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Operational tables (P1.2c): job, inbound_update, pending_action, llm_audit,
system_flag. TARGET_DESIGN §2 (Scheduler), §4.5 (approvals), §3 (llm_audit),
ADR-0008 (jobs), ADR-0009 (approval token).

- `job`: claimed with `SELECT ... FOR UPDATE SKIP LOCKED` over the partial
  index `idx_job_due`; `dedupe_key` UNIQUE makes scheduling idempotent.
- `inbound_update`: Telegram `update_id` UNIQUE -> `INSERT .. ON CONFLICT DO
  NOTHING` is the replay guard. Append-only except processed_at/event_id.
- `pending_action`: payload + sha256 + nonce + expiry; single-use enforced in
  the DB (`nimbus_single_use_guard`: no change once status != 'pending').
- `llm_audit`: append-only; metadata + cost always, redacted payload only as
  `crypto`-encrypted bytes (INV-7), purged to NULL after 14 days.
- `system_flag`: key/value (kill switch, spend mode, poll offset). Mutable.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)
_UUID_PK_DEFAULT = text("gen_random_uuid()")
_NOW = text("now()")


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed', 'dead', 'cancelled')",
            name="ck_job_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_job_attempts"),
        Index("idx_job_due", "run_at", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=_UUID_PK_DEFAULT
    )
    kind: Mapped[str] = mapped_column(nullable=False)
    run_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    dedupe_key: Mapped[str] = mapped_column(nullable=False, unique=True)
    rrule: Mapped[str | None] = mapped_column(nullable=True)
    proactive: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    last_error: Mapped[str | None] = mapped_column(nullable=True)  # sanitised by caller
    locked_by: Mapped[str | None] = mapped_column(nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)


class InboundUpdate(Base):
    __tablename__ = "inbound_update"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=_UUID_PK_DEFAULT
    )
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    from_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kind: Mapped[str] = mapped_column(nullable=False)
    received_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)
    processed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=True
    )


class PendingAction(Base):
    __tablename__ = "pending_action"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'consumed', 'expired', 'cancelled', 'rejected')",
            name="ck_pending_action_status",
        ),
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_pending_action_sha256"),
        CheckConstraint("expires_at > created_at", name="ck_pending_action_expiry"),
        CheckConstraint(
            "(status = 'consumed') = (consumed_at IS NOT NULL)",
            name="ck_pending_action_consumed_at",
        ),
        Index("idx_pending_action_status_expiry", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(  # the action_id carried by the approval token
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=_UUID_PK_DEFAULT
    )
    tool: Mapped[str] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    requires_totp: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    expires_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)
    consumed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=True
    )


class LLMAudit(Base):
    __tablename__ = "llm_audit"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'blocked', 'error')", name="ck_llm_audit_status"),
        Index("idx_llm_audit_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=_UUID_PK_DEFAULT
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)
    purpose: Mapped[str] = mapped_column(nullable=False)
    provider: Mapped[str] = mapped_column(nullable=False)
    model: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_inr: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=True
    )
    # Versioned ciphertext from src/db/crypto.py; NULLed by the 14-day purge.
    payload_redacted_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    payload_expires_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)


class SystemFlag(Base):
    __tablename__ = "system_flag"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)
    updated_by: Mapped[str | None] = mapped_column(nullable=True)
