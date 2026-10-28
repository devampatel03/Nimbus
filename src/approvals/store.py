# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Postgres side of approvals: `pending_action` rows and `policy_audit` rows.

Each method runs in its own short transaction, so a consumed approval is
COMMITTED before the tool runs (a crash after the consume burns the approval
and runs nothing: at-most-once, fail closed).

`consume` is the single atomic `pending -> consumed` transition. Its WHERE
clause re-checks, inside the same statement, everything the service already
checked: still pending, not expired at `now`, same payload hash, and the
TOTP step-up when the row demands it. Two concurrent consumes cannot both
succeed (row lock on UPDATE; the `nimbus_single_use_guard` trigger refuses any
change once the row has left 'pending'). Only `consume` mints a
`ConsumedApproval` (via `seal_consumed`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.approvals.types import AuditRecord, ConsumedApproval, seal_consumed
from src.memory.schemas.ops import PendingAction, PolicyAudit

TOTP_FAILED = "totp_failed"
TOTP_OK = "totp_ok"
_CLOSE_STATUSES = frozenset({"expired", "cancelled", "rejected"})


@dataclass(frozen=True)
class PendingRow:
    action_id: uuid.UUID
    tool: str
    action: str
    payload: Mapping[str, Any]
    payload_sha256: str
    nonce: bytes
    requires_totp: bool
    status: str
    expires_at: datetime
    created_at: datetime


def _cap(value: str | None, n: int) -> str | None:
    if value is None:
        return None
    return str(value)[:n].encode("unicode_escape").decode("ascii")[:n]


class ApprovalStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sf = sessions

    async def create(
        self,
        *,
        tool: str,
        action: str,
        payload: Mapping[str, Any],
        payload_sha256: str,
        nonce: bytes,
        requires_totp: bool,
        created_at: datetime,
        expires_at: datetime,
        source_event_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        action_id = uuid.uuid4()
        async with self._sf() as s, s.begin():
            await s.execute(
                insert(PendingAction).values(
                    id=action_id,
                    tool=tool,
                    action=action,
                    payload=dict(payload),
                    payload_sha256=payload_sha256,
                    nonce=nonce,
                    requires_totp=requires_totp,
                    status="pending",
                    created_at=created_at,
                    expires_at=expires_at,
                    source_event_id=source_event_id,
                )
            )
        return action_id

    async def load(self, action_id: uuid.UUID) -> PendingRow | None:
        async with self._sf() as s:
            row = (
                await s.execute(select(PendingAction).where(PendingAction.id == action_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            return PendingRow(
                row.id,
                row.tool,
                row.action,
                row.payload,
                row.payload_sha256,
                bytes(row.nonce),
                bool(row.requires_totp),
                row.status,
                row.expires_at,
                row.created_at,
            )

    async def open_count(self, now: datetime) -> int:
        async with self._sf() as s:
            n = await s.scalar(
                select(func.count())
                .select_from(PendingAction)
                .where(PendingAction.status == "pending", PendingAction.expires_at > now)
            )
            return int(n or 0)

    async def open_duplicate(self, payload_sha256: str, now: datetime) -> uuid.UUID | None:
        async with self._sf() as s:
            found = await s.scalar(
                select(PendingAction.id)
                .where(
                    PendingAction.status == "pending",
                    PendingAction.expires_at > now,
                    PendingAction.payload_sha256 == payload_sha256,
                )
                .limit(1)
            )
            return found

    async def consume(
        self,
        action_id: uuid.UUID,
        *,
        payload_sha256: str,
        now: datetime,
        step_up_verified: bool,
    ) -> ConsumedApproval | None:
        """Atomic `pending -> consumed`. None when any condition fails."""
        su = step_up_verified is True
        conds = [
            PendingAction.id == action_id,
            PendingAction.status == "pending",
            PendingAction.expires_at > now,
            PendingAction.payload_sha256 == payload_sha256,
        ]
        if not su:  # a row that demands the step-up cannot be consumed without it
            conds.append(PendingAction.requires_totp.is_(False))
        stmt = (
            update(PendingAction)
            .where(*conds)
            .values(status="consumed", consumed_at=now)
            .returning(
                PendingAction.id,
                PendingAction.tool,
                PendingAction.action,
                PendingAction.payload,
                PendingAction.payload_sha256,
                PendingAction.requires_totp,
                PendingAction.source_event_id,
            )
        )
        async with self._sf() as s, s.begin():
            row = (await s.execute(stmt)).first()
        if row is None:
            return None
        return seal_consumed(
            action_id=row[0],
            tool=row[1],
            action=row[2],
            payload=row[3],
            payload_sha256=row[4],
            step_up_verified=bool(row[5]) and su,
            source_event_id=row[6],
        )

    async def close(self, action_id: uuid.UUID, status: str) -> bool:
        """pending -> expired / cancelled / rejected. False if no longer pending."""
        if status not in _CLOSE_STATUSES:
            raise ValueError("bad close status")
        async with self._sf() as s, s.begin():
            row = (
                await s.execute(
                    update(PendingAction)
                    .where(PendingAction.id == action_id, PendingAction.status == "pending")
                    .values(status=status)
                    .returning(PendingAction.id)
                )
            ).first()
        return row is not None

    async def audit(self, rec: AuditRecord, *, now: datetime | None = None) -> None:
        values: dict[str, Any] = {
            "kind": rec.kind,
            "event": _cap(rec.event, 32),
            "tool": _cap(rec.tool, 64),
            "rule_id": _cap(rec.rule_id, 64),
            "outcome": _cap(rec.outcome, 32),
            "reason": _cap(rec.reason, 64),
            "tainted": rec.tainted,
            "step_up": rec.step_up,
            "executed": rec.executed,
            "action_id": rec.action_id,
            "totp_step": rec.totp_step,
        }
        if now is not None:
            values["created_at"] = now
        async with self._sf() as s, s.begin():
            await s.execute(insert(PolicyAudit).values(**values))

    async def totp_failures_since(self, since: datetime) -> int:
        async with self._sf() as s:
            n = await s.scalar(
                select(func.count())
                .select_from(PolicyAudit)
                .where(PolicyAudit.event == TOTP_FAILED, PolicyAudit.created_at > since)
            )
            return int(n or 0)

    async def last_totp_step(self) -> int | None:
        async with self._sf() as s:
            return await s.scalar(
                select(func.max(PolicyAudit.totp_step)).where(PolicyAudit.event == TOTP_OK)
            )
