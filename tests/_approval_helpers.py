# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Shared harness for the P3.2 approval tests (integration, adversarial, eval).

Each harness runs in its OWN Postgres schema (created and dropped per test),
so global counters (open cards, TOTP failures, last TOTP step) never leak
between tests. Everything below the dispatcher is real: `ApprovalStore` on
Postgres, `ApprovalService`, `Dispatcher` with `PolicyEngine`. Only Telegram
(InMemoryGateway), the clock and the tools (spies) are fakes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from config.settings import settings
from src.app.fakes import InMemoryGateway
from src.approvals import token as tok
from src.approvals import totp
from src.approvals.service import ApprovalService
from src.approvals.settings import ApprovalSettings
from src.approvals.store import ApprovalStore
from src.gateway.types import ApprovalCard, InboundUpdate, UpdateKind
from src.llm.types import ToolSpec
from src.memory.schemas import Base
from src.orchestrator.dispatch import Dispatcher, ExecutionCounter, ToolOutput
from src.policy import PolicyEngine, Risk

USER = 777_000_111
OTHER_USER = 777_000_222
HMAC_KEY = "test-hmac-key-" + "x" * 40  # pragma: allowlist secret
TOTP_B32 = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret
T0 = datetime(2030, 1, 15, 6, 0, 3, tzinfo=UTC)

# Spy tools named like real policy-registry rows (risk must match the registry).
SPY_TOOLS: dict[str, Risk] = {
    "tasks_update": Risk.WRITE_INTERNAL,  # allow untainted, approval when tainted
    "tasks_delete": Risk.WRITE_INTERNAL,  # destructive: approval + TOTP step-up
    "tasks_list": Risk.READ,
}


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


class SpyTool:
    def __init__(self, name: str, risk: Risk) -> None:
        self.spec = ToolSpec(name=name, description=name)
        self.risk = risk
        self.calls: list[dict[str, Any]] = []

    @property
    def runs(self) -> int:
        return len(self.calls)

    async def run(self, args: Mapping[str, Any]) -> ToolOutput:
        self.calls.append(dict(args))
        return ToolOutput("done", untrusted=False)


@asynccontextmanager
async def isolated_schema() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    schema = f"ap_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        settings.postgres_dsn,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    try:
        async with engine.begin() as conn:
            # checkfirst=False: the session's tables in `public` are visible on
            # the search_path and would otherwise be reused instead of created.
            await conn.run_sync(lambda c: Base.metadata.create_all(c, checkfirst=False))
            got = await conn.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname = :s"), {"s": schema}
            )
            assert got.scalar_one() == len(Base.metadata.tables), "schema not isolated"
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def approval_settings(*, totp_on: bool = True, **over: Any) -> ApprovalSettings:
    return ApprovalSettings(
        _env_file=None,  # type: ignore[call-arg]
        hmac_key=SecretStr(HMAC_KEY),
        totp_secret=SecretStr(TOTP_B32) if totp_on else None,
        **over,
    )


@dataclass
class Harness:
    sf: async_sessionmaker[AsyncSession]
    gw: InMemoryGateway
    clock: FakeClock
    store: ApprovalStore
    service: ApprovalService
    dispatcher: Dispatcher
    tools: dict[str, SpyTool]
    settings: ApprovalSettings
    _next: int = field(default=1)

    @property
    def exposed(self) -> frozenset[str]:
        return frozenset(self.tools)

    async def propose(self, tool: str, args: Mapping[str, Any], *, tainted: bool = True) -> Any:
        return await self.dispatcher.dispatch(tool, args, exposed=self.exposed, tainted=tainted)

    def last_card(self) -> ApprovalCard:
        return self.gw.cards[-1]

    def callback(
        self, data: str | None, *, user: int | None = USER, message_id: int | None = 5000
    ) -> InboundUpdate:
        self._next += 1
        return InboundUpdate(
            update_id=10_000 + self._next,
            kind=UpdateKind.CALLBACK,
            message_id=message_id,
            callback_id=f"cb{self._next}",
            callback_data=data,
            from_user_id=user,
        )

    async def tap(self, data: str | None, **kw: Any) -> str | None:
        """Deliver a callback; returns the toast text answered (None = no answer)."""
        before = len(self.gw.callbacks)
        await self.service.handle_callback(self.callback(data, **kw))
        if len(self.gw.callbacks) == before:
            return None
        return self.gw.callbacks[-1][1]

    def keypad_data(self, key: str) -> str:
        rows = self.gw.keypads[-1]
        for row in rows:
            for _, data in row:
                if data.endswith(f":{key}"):
                    return data
        raise LookupError(key)

    async def enter_code(self, code: str) -> str | None:
        answer: str | None = None
        for d in code:
            answer = await self.tap(self.keypad_data(d))
        return answer

    def current_code(self, step_offset: int = 0) -> str:
        secret = totp.decode_secret(TOTP_B32)
        return totp.code_at(secret, totp.step_of(self.clock.now().timestamp()) + step_offset)

    def wrong_code(self) -> str:
        good = {self.current_code(o) for o in (-1, 0, 1)}
        for n in range(1_000_000):
            c = f"{n:06d}"
            if c not in good:
                return c
        raise AssertionError("unreachable")

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.sf() as s:
            return list((await s.execute(text(sql), params)).all())

    async def status(self, action_id: uuid.UUID) -> str:
        (row,) = await self.rows("SELECT status FROM pending_action WHERE id = :i", i=action_id)
        return str(row[0])

    async def only_action_id(self) -> uuid.UUID:
        """The row behind the last card sent (or the single row if no card)."""
        if self.gw.cards:
            return tok.parse(self.gw.cards[-1].approve_data).action_id
        rows = await self.rows("SELECT id FROM pending_action")
        assert len(rows) == 1
        return uuid.UUID(str(rows[0][0]))


def build_harness(
    sf: async_sessionmaker[AsyncSession],
    *,
    totp_on: bool = True,
    policy: Any = None,
    **settings_over: Any,
) -> Harness:
    clock = FakeClock()
    gw = InMemoryGateway()
    ap = approval_settings(totp_on=totp_on, **settings_over)
    store = ApprovalStore(sf)
    service = ApprovalService(store, gw, ap, user_id=USER, clock=clock)
    tools = {n: SpyTool(n, r) for n, r in SPY_TOOLS.items()}
    dispatcher = Dispatcher(
        tools,
        policy or PolicyEngine(),
        approvals=service,
        audit=store,
        # The per-tool hourly rate limit follows the fake clock too.
        counter=ExecutionCounter(clock=lambda: clock.now().timestamp()),
    )
    service.bind_executor(dispatcher)
    return Harness(sf, gw, clock, store, service, dispatcher, tools, ap)
