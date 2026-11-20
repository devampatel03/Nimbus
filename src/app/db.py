# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Engine and session factory for the app process.

Two hardening settings (INV-7, P1.4b follow-up):

* `hide_parameters=True`: SQLAlchemy errors and logs never include bound
  parameter values (user text, tokens). The statement text is still included.
* a server-side `statement_timeout`: a hung query (for example an audit write
  while Postgres is wedged) raises instead of blocking a loop forever.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(dsn: str, *, statement_timeout_s: float) -> AsyncEngine:
    millis = max(1, int(statement_timeout_s * 1000))
    return create_async_engine(
        dsn,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"server_settings": {"statement_timeout": str(millis)}},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
