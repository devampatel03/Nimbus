# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-7 / P1.4b follow-ups: SQL parameters never reach error text, and no
statement can run forever. Uses the per-session test database."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from config.settings import settings
from src.app.db import build_engine

SECRET = "PARAM-SECRET-VALUE-93f1"  # pragma: allowlist secret
BAD_SQL = text("SELECT :x || 'a' FROM table_that_does_not_exist_zz")


async def test_app_engine_hides_parameters_in_errors() -> None:
    eng = build_engine(settings.postgres_dsn, statement_timeout_s=30)
    try:
        async with eng.connect() as conn:
            with pytest.raises(ProgrammingError) as ei:
                await conn.execute(BAD_SQL, {"x": SECRET})
    finally:
        await eng.dispose()
    assert SECRET not in str(ei.value) and "[parameters:" not in str(ei.value)
    assert "table_that_does_not_exist_zz" in str(ei.value)  # statement text is still useful


async def test_shared_engine_hides_parameters_too() -> None:
    from src.db.postgres import engine

    try:
        async with engine.connect() as conn:
            with pytest.raises(ProgrammingError) as ei:
                await conn.execute(BAD_SQL, {"x": SECRET})
    finally:
        await engine.dispose()
    assert SECRET not in str(ei.value) and "[parameters:" not in str(ei.value)


async def test_statement_timeout_stops_a_hung_query() -> None:
    eng = build_engine(settings.postgres_dsn, statement_timeout_s=0.3)
    try:
        async with eng.connect() as conn:
            with pytest.raises(DBAPIError) as ei:
                await conn.execute(text("SELECT pg_sleep(10)"))
    finally:
        await eng.dispose()
    assert "statement timeout" in str(ei.value).lower() or "canceling" in str(ei.value).lower()


async def test_fast_queries_are_unaffected_by_the_timeout() -> None:
    eng = build_engine(settings.postgres_dsn, statement_timeout_s=5)
    try:
        async with eng.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await eng.dispose()
