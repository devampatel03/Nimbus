# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from config.settings import settings
from src.app import runtime as rt
from src.memory.schemas import Base
from tests._test_db import PREFIX
from tests.integration.app import harness


@pytest.fixture(scope="module")
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """A scratch database for this module. The operational tables carry
    append-only triggers (TRUNCATE/DELETE are refused), so tests share it and use
    disjoint update-id ranges instead of cleaning up."""
    from tests.conftest import _create_db, _drop_db

    name = f"{PREFIX}{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    await _create_db(name)
    base = settings.postgres_dsn.rsplit("/", 1)[0]
    engine = create_async_engine(f"{base}/{name}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        await _drop_db(name)


@pytest.fixture(autouse=True)
def _point_harness_at_scratch_db(app_engine: AsyncEngine) -> Iterator[None]:
    harness.CURRENT_DSN = app_engine.url.render_as_string(hide_password=False)
    yield
    harness.CURRENT_DSN = ""


@pytest.fixture(autouse=True)
def card_dev_mode_and_scrub_cleanup(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests run with the operator dev switch (the repo card is not yet approved,
    H1) and undo the process-wide log scrubber afterwards."""
    monkeypatch.setenv("NIMBUS_CARD_DEV_MODE", "1")
    restores: list[Any] = []
    real = rt.install_log_scrubbing

    def spy(secrets: Any = ()) -> Any:
        restores.append(real(secrets))
        return restores[-1]

    monkeypatch.setattr(rt, "install_log_scrubbing", spy)
    yield
    for undo in restores:
        undo()
