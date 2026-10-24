# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Postgres flag/audit/ledger stores against a migrated scratch database.
Skipped when Postgres is not reachable (needs `docker compose up`)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.settings import settings
from src.db.crypto import DataCipher, generate_key
from src.llm.fake import FakeLLM
from src.llm.guard import KILL_SWITCH_KEY, SPEND_MODE_KEY, GuardedLLM
from src.llm.pg_stores import PostgresAuditStore, PostgresFlagStore
from src.llm.protocols import AuditRecord
from src.llm.settings import ModelPrice
from src.llm.types import ChatMessage, ChatRequest, Lane, LLMMode, LLMRefused, Purpose
from tests.fakes.llm import FakeClock, llm_settings

ROOT = Path(__file__).resolve().parents[3]


def _dsn_for(db: str) -> str:
    return settings.postgres_dsn.rsplit("/", 1)[0] + f"/{db}"


async def _admin(sql: str) -> None:
    engine = create_async_engine(
        _dsn_for("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def migrated_dsn() -> Iterator[str]:
    name = f"nimbus_llmtest_{uuid.uuid4().hex[:10]}"
    try:
        asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    except Exception:  # noqa: BLE001
        pytest.skip("postgres not reachable")
    try:
        dsn = _dsn_for(name)
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", dsn)
        command.upgrade(cfg, "head")
        yield dsn
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
async def engine(migrated_dsn: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_dsn, poolclass=NullPool)
    yield eng
    await eng.dispose()


def _record(created_at: datetime, cost: str = "1", **kw: object) -> AuditRecord:
    return AuditRecord(
        purpose="chat",
        provider="p",
        model="m",
        status="ok",
        created_at=created_at,
        cost_inr=Decimal(cost),
        tokens_in=3,
        tokens_out=4,
        latency_ms=12,
        **kw,  # type: ignore[arg-type]
    )


async def test_flag_store_upsert_and_get(engine: AsyncEngine) -> None:
    flags = PostgresFlagStore(async_sessionmaker(engine, expire_on_commit=False))
    assert await flags.get("k") is None
    await flags.set("k", {"engaged": True}, updated_by="a")
    await flags.set("k", {"engaged": False, "n": 2}, updated_by="b")
    assert await flags.get("k") == {"engaged": False, "n": 2}


async def test_audit_store_writes_row_and_sums_spend_by_window(engine: AsyncEngine) -> None:
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = PostgresAuditStore(sf)
    t0 = datetime(2031, 1, 1, 6, 0, tzinfo=UTC)
    await store.write(_record(t0, "1.5"))
    await store.write(_record(t0 + timedelta(hours=1), "2.25"))
    await store.write(_record(t0 + timedelta(days=2), "100"))

    assert await store.spent_between(t0 - timedelta(hours=1), t0 + timedelta(days=1)) == Decimal(
        "3.75"
    )
    assert await store.spent_between(t0 + timedelta(days=5), t0 + timedelta(days=6)) == Decimal(0)


async def test_payload_is_encrypted_with_retention_and_dropped_without_cipher(
    engine: AsyncEngine,
) -> None:
    sf = async_sessionmaker(engine, expire_on_commit=False)
    cipher = DataCipher.from_key(os.urandom(32))
    when = datetime(2032, 1, 1, tzinfo=UTC)
    enc_id = await PostgresAuditStore(sf, cipher=cipher, retention_days=14).write(
        _record(when, payload_redacted="user: hello REDACTED")
    )
    plain_id = await PostgresAuditStore(sf).write(_record(when, payload_redacted="user: hello"))

    async with engine.connect() as conn:
        rows = {
            r.id: r
            for r in (
                await conn.execute(
                    text("SELECT id, payload_redacted_enc, payload_expires_at FROM llm_audit")
                )
            ).all()
        }
    assert b"hello" not in bytes(rows[enc_id].payload_redacted_enc)
    assert rows[enc_id].payload_expires_at == when + timedelta(days=14)
    assert rows[plain_id].payload_redacted_enc is None
    assert generate_key()  # helper still importable (key format check)


async def test_guard_end_to_end_on_postgres(engine: AsyncEngine) -> None:
    sf = async_sessionmaker(engine, expire_on_commit=False)
    flags, audit = PostgresFlagStore(sf), PostgresAuditStore(sf)
    clock = FakeClock(datetime(2033, 3, 3, 4, 30, tzinfo=UTC))
    alerts: list[str] = []

    async def alert(msg: str) -> None:
        alerts.append(msg)

    llm = GuardedLLM(
        FakeLLM(default="hi"),
        flags=flags,
        audit=audit,
        ledger=audit,
        settings=llm_settings(
            daily_spend_cap_inr="0.1",
            price_table={
                "*": ModelPrice(in_per_mtok_inr=Decimal(5000), out_per_mtok_inr=Decimal(5000))
            },
        ),
        alert=alert,
        clock=clock,
    )
    req = ChatRequest(
        purpose=Purpose.CHAT,
        lane=Lane.TRUSTED,
        messages=(ChatMessage(role="user", content="hello"),),
        max_output_tokens=10,
    )

    assert (await llm.chat(req)).text == "hi"  # first call fits the (tiny) cap
    with pytest.raises(LLMRefused) as ei:
        await llm.chat(req)  # spent 0.0001+ => over cap
    assert ei.value.reason == "spend_cap"
    assert await llm.mode() is LLMMode.READ_ONLY
    assert (await flags.get(SPEND_MODE_KEY) or {})["mode"] == "read_only"
    assert len(alerts) == 1

    await flags.set(KILL_SWITCH_KEY, {"engaged": True}, updated_by="test")
    assert await llm.mode() is LLMMode.KILLED
    async with engine.connect() as conn:
        n = (
            await conn.execute(text("SELECT count(*) FROM llm_audit WHERE status='blocked'"))
        ).scalar_one()
    assert n >= 1
