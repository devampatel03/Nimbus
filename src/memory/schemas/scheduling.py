# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Persisted reminders and tasks (P3.3, ADR-0008).

A reminder and its delivery `job` are written in ONE transaction
(`src.scheduler.reminders`), so a reminder can never exist without being
scheduled. Times are UTC; IST is a display concern (`src.core.timeparse`).
Neither table is append-only: reminders and tasks are the user's own mutable
working state, not memory facts.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.memory.schemas.base import Base

_TZ = DateTime(timezone=True)
_NOW = text("now()")
_UUID_PK_DEFAULT = text("gen_random_uuid()")


class Reminder(Base):
    __tablename__ = "reminder"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'fired')", name="ck_reminder_status"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 500", name="ck_reminder_title"),
        Index("idx_reminder_status_at", "status", "remind_at"),
        # One reminder per (turn, title, time): a replayed turn cannot duplicate it (M5).
        Index(
            "uq_reminder_source_title_at",
            "source_event_id",
            "title",
            "remind_at",
            unique=True,
            postgresql_where=text("source_event_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=_UUID_PK_DEFAULT
    )
    title: Mapped[str] = mapped_column(nullable=False)
    remind_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    fired_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint("char_length(title) BETWEEN 1 AND 500", name="ck_task_title"),
        CheckConstraint("(done_at IS NOT NULL) = done", name="ck_task_done_at"),
        Index("idx_task_done_created", "done", "created_at"),
        Index(
            "uq_task_source_title",
            "source_event_id",
            "title",
            unique=True,
            postgresql_where=text("source_event_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=_UUID_PK_DEFAULT
    )
    title: Mapped[str] = mapped_column(nullable=False)
    done: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    done_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=_NOW)
