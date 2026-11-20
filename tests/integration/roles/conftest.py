# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P6.1 fixtures: a scratch database built by the OWNER (ORM create_all, same
triggers as the migrations), a throwaway least-privilege role granted by the
production `apply_app_grants`, and an engine/session factory that connects AS
that role. Reuse `app_sf` to run an existing suite's operations under the app
role instead of the superuser (the parametrised-fixture seam)."""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from config.settings import settings
from src.db.roles import apply_app_grants, create_role
from src.memory.schemas import Base
from tests._test_db import PREFIX


@dataclass
class RoleDb:
    name: str
    role: str
    owner: AsyncEngine
    app: AsyncEngine
    app_sf: async_sessionmaker[AsyncSession]
    owner_sf: async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
async def roledb() -> AsyncIterator[RoleDb]:
    from tests.conftest import _create_db, _drop_db

    name = f"{PREFIX}{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    role = f"nimbus_app_t{uuid.uuid4().hex[:10]}"
    pw = secrets.token_hex(16)
    await _create_db(name)
    base = settings.postgres_dsn.rsplit("/", 1)[0]
    owner = create_async_engine(f"{base}/{name}", poolclass=NullPool)
    admin = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    app: AsyncEngine | None = None
    try:
        async with owner.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await create_role(conn, role, pw)
            await apply_app_grants(conn, role, database=name)
            await apply_app_grants(conn, role, database=name)  # idempotent
        host = f"{settings.postgres_host}:{settings.postgres_port}"
        app = create_async_engine(
            f"postgresql+asyncpg://{role}:{pw}@{host}/{name}", pool_size=10, hide_parameters=True
        )
        yield RoleDb(
            name,
            role,
            owner,
            app,
            async_sessionmaker(app, expire_on_commit=False),
            async_sessionmaker(owner, expire_on_commit=False),
        )
    finally:
        if app is not None:
            await app.dispose()
        await owner.dispose()
        await _drop_db(name)
        async with admin.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await admin.dispose()
