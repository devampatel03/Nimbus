# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.4 database canary sweep against real Postgres (isolated schema): finds a
plain, a base64 and a JSON-nested canary and a literal secret, reports counts
only (no value, no row id), and a clean database is clean."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.canary.detect import CanaryDetector
from src.canary.sweep import sweep_database
from src.canary.tokens import Site
from src.memory.schemas import Base
from tests._approval_helpers import isolated_schema
from tests._canary_helpers import canaries

CS = canaries()
SECRET = "literal-secret-value-0123456789"  # pragma: allowlist secret


@pytest.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with isolated_schema() as (_, factory):
        yield factory


def detector() -> CanaryDetector:
    return CanaryDetector([(c.site.value, c.value, True) for c in CS])


async def _flag(sf: async_sessionmaker[AsyncSession], key: str, value: object) -> None:
    async with sf() as s, s.begin():
        await s.execute(
            text("INSERT INTO system_flag (key, value) VALUES (:k, CAST(:v AS jsonb))"),
            {"k": key, "v": json.dumps(value)},
        )


async def test_clean_database_is_clean(sf: async_sessionmaker[AsyncSession]) -> None:
    await _flag(sf, "llm_kill_switch", {"on": False})
    async with sf() as s:
        report = await sweep_database(s, detector(), metadata=Base.metadata, literals=(SECRET,))
    assert report.clean and report.rows >= 1 and report.tables >= 5
    assert report.summary().endswith(" clean")


async def test_sweep_finds_obfuscated_canaries_and_literal_secrets(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    await _flag(sf, "a", {"note": CS.value(Site.SYSTEM)})
    await _flag(
        sf, "b", {"deep": [{"x": base64.b64encode(CS.value(Site.MEMORY).encode()).decode()}]}
    )
    await _flag(sf, f"c-{' '.join(CS.value(Site.TOOL_FRAME))}", {"ok": True})
    await _flag(sf, "d", {"token": SECRET})
    async with sf() as s:
        report = await sweep_database(s, detector(), metadata=Base.metadata, literals=(SECRET,))
    found = {(f.table, f.column, f.category): f.rows for f in report.findings}
    assert found == {
        ("system_flag", "value", "canary"): 2,
        ("system_flag", "key", "canary"): 1,
        ("system_flag", "value", "secret"): 1,
    }
    summary = report.summary()
    assert "FOUND" in summary and "system_flag.value:canary=2" in summary
    for v in (*CS.values(), SECRET):
        assert v not in summary and v not in repr(report)
