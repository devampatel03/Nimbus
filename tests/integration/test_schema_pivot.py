# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P1.2c pivot schema, tested on a MIGRATED scratch database (real Postgres):
catalog assertions (CHECK/UNIQUE/vector/HNSW, which compare_metadata ignores),
append-only triggers via raw SQL (INV-3 / R7), R8 promotion rule, job SKIP
LOCKED claiming, inbound replay dedupe, single-use approvals, downgrade
round-trip including data that pre-dates the migration.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from config.settings import settings

ROOT = Path(__file__).resolve().parents[2]
PREV_REV = "a1c3f5e7b9d2"  # pragma: allowlist secret
VEC = "[" + ",".join(["0.1"] * 1024) + "]"


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


def _cfg(dsn: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


@pytest.fixture(scope="module")
def migrated_dsn() -> Iterator[str]:
    name = f"nimbus_schematest_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        dsn = _dsn_for(name)
        command.upgrade(_cfg(dsn), "head")
        yield dsn
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
async def engine(migrated_dsn: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_dsn, poolclass=NullPool)
    yield eng
    await eng.dispose()


async def run(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    """Execute in its own committed transaction; return rows (if any)."""
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return list(result.all()) if result.returns_rows else []


async def fails(engine: AsyncEngine, sql: str, **params: Any) -> str:
    with pytest.raises((IntegrityError, DBAPIError)) as exc:
        await run(engine, sql, **params)
    return str(exc.value)


# ---- row builders -------------------------------------------------------------------------


async def mk_event(engine: AsyncEngine, lane: str = "trusted") -> uuid.UUID:
    eid = uuid.uuid4()
    await run(
        engine,
        "INSERT INTO event (id, source_key, kind, source, lane, occurred_at) "
        "VALUES (:id, :k, 'text', 'telegram', :lane, now())",
        id=eid,
        k=f"tg:update:{eid}",
        lane=lane,
    )
    return eid


async def mk_chunk(
    engine: AsyncEngine, event_id: uuid.UUID, lane: str = "trusted", promoted: bool = True
) -> uuid.UUID:
    cid = uuid.uuid4()
    await run(
        engine,
        "INSERT INTO memory_chunk (id, source_event_id, content, embedding, model_id, lane, "
        "promoted) VALUES (:id, :e, 'hello', CAST(:v AS vector), 'qwen3-0.6b', :lane, :p)",
        id=cid,
        e=event_id,
        v=VEC,
        lane=lane,
        p=promoted,
    )
    return cid


async def mk_session(engine: AsyncEngine) -> uuid.UUID:
    sid = uuid.uuid4()
    await run(
        engine,
        "INSERT INTO session (id, user_id, mode, started_at, device_id) "
        "VALUES (:id, :u, 'b', now(), 'd')",
        id=sid,
        u=uuid.uuid4(),
    )
    return sid


async def mk_edge(engine: AsyncEngine) -> uuid.UUID:
    nid, eid, sid = uuid.uuid4(), uuid.uuid4(), await mk_session(engine)
    await run(
        engine,
        "INSERT INTO graph_node (id, entity_type, canonical_name, created_at) "
        "VALUES (:id, 'person', 'x', now())",
        id=nid,
    )
    await run(
        engine,
        "INSERT INTO graph_edge (id, subject_id, predicate, object_literal, valid_from, "
        "ingested_at, source_session, confidence, lane) "
        "VALUES (:id, :n, 'lives_in', 'Pune', now(), now(), :s, 0.9, 'trusted')",
        id=eid,
        n=nid,
        s=sid,
    )
    return eid


# ---- catalog assertions -----------------------------------------------------------------


async def test_all_seven_tables_exist(engine: AsyncEngine) -> None:
    rows = await run(engine, "SELECT tablename FROM pg_tables WHERE schemaname='public'")
    tables = {r[0] for r in rows}
    assert {
        "event",
        "memory_chunk",
        "job",
        "llm_audit",
        "system_flag",
        "pending_action",
        "inbound_update",
    } <= tables


async def test_expected_check_constraints_exist(engine: AsyncEngine) -> None:
    rows = await run(engine, "SELECT conname FROM pg_constraint WHERE contype='c'")
    names = {r[0] for r in rows}
    expected = {
        "ck_event_lane",
        "ck_event_confidence",
        "ck_memory_chunk_lane",
        "ck_memory_chunk_confidence",
        "ck_memory_chunk_untrusted_promotion",
        "ck_memory_chunk_dim",
        "ck_graph_edge_lane",
        "ck_graph_edge_untrusted_promotion",
        "ck_commitment_lane",
        "ck_commitment_untrusted_promotion",
        "ck_decision_lane",
        "ck_decision_untrusted_promotion",
        "ck_decision_confidence",
        "ck_job_status",
        "ck_pending_action_status",
        "ck_pending_action_sha256",
        "ck_memory_chunk_not_self_superseded",
        "ck_pending_action_expiry",
        "ck_pending_action_consumed_at",
        "ck_llm_audit_status",
    }
    assert expected <= names, expected - names


async def test_unique_constraints_exist(engine: AsyncEngine) -> None:
    rows = await run(
        engine,
        "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE contype='u'",
    )
    defs = {(r[0], r[1]) for r in rows}
    assert ("event", "UNIQUE (source_key)") in defs
    assert ("job", "UNIQUE (dedupe_key)") in defs
    assert ("inbound_update", "UNIQUE (update_id)") in defs


async def test_embedding_is_vector_1024_with_hnsw_cosine_index(engine: AsyncEngine) -> None:
    typ = await run(
        engine,
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid='memory_chunk'::regclass AND attname='embedding'",
    )
    assert typ[0][0] == "vector(1024)"
    idx = await run(
        engine, "SELECT indexdef FROM pg_indexes WHERE indexname='idx_memory_chunk_embedding_hnsw'"
    )
    assert "USING hnsw" in idx[0][0] and "vector_cosine_ops" in idx[0][0]


async def test_provenance_columns_are_not_null(engine: AsyncEngine) -> None:
    rows = await run(
        engine,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND is_nullable='NO' "
        "AND column_name IN ('lane','confidence','created_at','source_event_id')",
    )
    notnull = {(r[0], r[1]) for r in rows}
    for table in ("event", "memory_chunk", "graph_edge", "commitment", "decision"):
        assert (table, "lane") in notnull, table
    for table in ("event", "memory_chunk", "commitment", "decision"):
        assert (table, "confidence") in notnull, table
    assert ("memory_chunk", "source_event_id") in notnull


async def test_triggers_installed(engine: AsyncEngine) -> None:
    rows = await run(
        engine, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'trg_%'"
    )
    names = {r[0] for r in rows}
    for table in (
        "event",
        "llm_audit",
        "inbound_update",
        "memory_chunk",
        "graph_edge",
        "commitment",
        "decision",
        "pending_action",
    ):
        assert f"trg_{table}_append_only" in names, table
    assert "trg_pending_action_single_use" in names


# ---- INV-3 / R7: append-only via raw SQL ------------------------------------------------


async def test_event_is_fully_immutable(engine: AsyncEngine) -> None:
    eid = await mk_event(engine)
    assert "INV-3" in await fails(engine, "UPDATE event SET body='x' WHERE id=:i", i=eid)
    assert "INV-3" in await fails(engine, "UPDATE event SET lane='untrusted' WHERE id=:i", i=eid)
    assert "INV-3" in await fails(engine, "DELETE FROM event WHERE id=:i", i=eid)


async def test_memory_chunk_content_immutable_supersession_allowed(engine: AsyncEngine) -> None:
    eid = await mk_event(engine)
    old, new = await mk_chunk(engine, eid), None
    eid2 = await mk_event(engine)
    new = await mk_chunk(engine, eid2)
    for col_sql in (
        "content='tampered'",
        "embedding=CAST(:v AS vector)",
        "lane='untrusted'",
        "confidence=0.1",
        "model_id='other'",
        "source_event_id=:e2",
    ):
        msg = await fails(
            engine,
            f"UPDATE memory_chunk SET {col_sql} WHERE id=:i",
            i=old,
            v=VEC.replace("0.1", "0.2"),
            e2=eid2,
        )
        assert "INV-3" in msg, col_sql
    assert "INV-3" in await fails(engine, "DELETE FROM memory_chunk WHERE id=:i", i=old)
    # allowed: supersession columns, once
    await run(
        engine,
        "UPDATE memory_chunk SET invalidated_at=now(), superseded_by=:n WHERE id=:i",
        i=old,
        n=new,
    )
    assert "set-once" in await fails(
        engine, "UPDATE memory_chunk SET invalidated_at=now() + interval '1 day' WHERE id=:i", i=old
    )
    assert "set-once" in await fails(
        engine, "UPDATE memory_chunk SET superseded_by=NULL WHERE id=:i", i=old
    )


async def test_graph_edge_inv3_trigger(engine: AsyncEngine) -> None:
    edge = await mk_edge(engine)
    for set_sql in (
        "object_literal='Delhi'",
        "valid_from=now() - interval '1 year'",
        "predicate='works_at'",
        "confidence=0.2",
        "source_session=gen_random_uuid()",
    ):
        assert "INV-3" in await fails(
            engine, f"UPDATE graph_edge SET {set_sql} WHERE id=:i", i=edge
        ), set_sql
    assert "INV-3" in await fails(engine, "DELETE FROM graph_edge WHERE id=:i", i=edge)
    await run(engine, "UPDATE graph_edge SET valid_to=now() WHERE id=:i", i=edge)
    assert "set-once" in await fails(
        engine, "UPDATE graph_edge SET valid_to=NULL WHERE id=:i", i=edge
    )


async def test_commitment_and_decision_content_immutable(engine: AsyncEngine) -> None:
    sid = await mk_session(engine)
    cid, did = uuid.uuid4(), uuid.uuid4()
    await run(
        engine,
        "INSERT INTO commitment (id, source_session, description, confidence, created_at, "
        "lane) VALUES (:i, :s, 'call mom', 0.8, now(), 'trusted')",
        i=cid,
        s=sid,
    )
    await run(
        engine,
        "INSERT INTO decision (id, source_session, decision_text, reasoning, created_at, "
        "review_at, lane) VALUES (:i, :s, 'd', 'r', now(), now(), 'trusted')",
        i=did,
        s=sid,
    )
    assert "INV-3" in await fails(
        engine, "UPDATE commitment SET description='x' WHERE id=:i", i=cid
    )
    assert "INV-3" in await fails(engine, "DELETE FROM commitment WHERE id=:i", i=cid)
    await run(engine, "UPDATE commitment SET status='closed', closed_at=now() WHERE id=:i", i=cid)
    assert "INV-3" in await fails(
        engine, "UPDATE decision SET decision_text='x' WHERE id=:i", i=did
    )
    assert "INV-3" in await fails(engine, "DELETE FROM decision WHERE id=:i", i=did)
    await run(
        engine, "UPDATE decision SET outcome='ok', outcome_captured_at=now() WHERE id=:i", i=did
    )


async def test_llm_audit_append_only_but_payload_purgeable(engine: AsyncEngine) -> None:
    aid = uuid.uuid4()
    await run(
        engine,
        "INSERT INTO llm_audit (id, purpose, provider, model, status, payload_redacted_enc) "
        "VALUES (:i, 'chat', 'gemini', 'm', 'ok', '\\x01'::bytea)",
        i=aid,
    )
    assert "INV-3" in await fails(engine, "UPDATE llm_audit SET cost_inr=5 WHERE id=:i", i=aid)
    assert "INV-3" in await fails(engine, "DELETE FROM llm_audit WHERE id=:i", i=aid)
    await run(engine, "UPDATE llm_audit SET payload_redacted_enc=NULL WHERE id=:i", i=aid)


# ---- R8 ---------------------------------------------------------------------------------


async def test_untrusted_cannot_be_promoted_without_approval(engine: AsyncEngine) -> None:
    eid = await mk_event(engine, lane="untrusted")
    # untrusted + promoted=true at insert is rejected by CHECK
    msg = await fails(
        engine,
        "INSERT INTO memory_chunk (source_event_id, content, embedding, model_id, lane, promoted) "
        "VALUES (:e, 'x', CAST(:v AS vector), 'm', 'untrusted', true)",
        e=eid,
        v=VEC,
    )
    # P4.6: the promotion-guard trigger fires before the CHECK; either refuses.
    assert "ck_memory_chunk_untrusted_promotion" in msg or "cannot be born promoted" in msg
    cid = await mk_chunk(engine, eid, lane="untrusted", promoted=False)
    msg = await fails(engine, "UPDATE memory_chunk SET promoted=true WHERE id=:i", i=cid)
    assert "ck_memory_chunk_untrusted_promotion" in msg or "promotion needs a consumed" in msg
    # a fabricated approval id is refused (trigger first, FK behind it) ...
    bad = await fails(
        engine,
        "UPDATE memory_chunk SET promoted=true, promoted_approval_id=gen_random_uuid() WHERE id=:i",
        i=cid,
    )
    assert "fk_memory_chunk_promoted_approval_id" in bad or "promotion needs a consumed" in bad
    # ... and (P4.6) so is a real but merely PENDING pending_action row: it must be
    # consumed, in time, and name exactly this chunk.
    aid = uuid.uuid4()
    await run(
        engine,
        "INSERT INTO pending_action (id, tool, action, payload, payload_sha256, nonce, "
        "expires_at) VALUES (:i, 'memory', 'promote', jsonb_build_object('item_id', "
        "CAST(:c AS text)), :h, decode('00', 'hex'), now() + interval '10 minutes')",
        i=aid,
        c=str(cid),
        h="c" * 64,
    )
    assert "promotion needs a consumed" in await fails(
        engine,
        "UPDATE memory_chunk SET promoted=true, promoted_approval_id=:a WHERE id=:i",
        i=cid,
        a=aid,
    )
    await run(
        engine,
        "UPDATE pending_action SET status='consumed', consumed_at=now() WHERE id=:i",
        i=aid,
    )
    await run(
        engine,
        "UPDATE memory_chunk SET promoted=true, promoted_approval_id=:a WHERE id=:i",
        i=cid,
        a=aid,
    )


@pytest.mark.parametrize("table", ["memory_chunk", "graph_edge", "commitment", "decision"])
async def test_promoted_approval_id_has_fk_to_pending_action(
    engine: AsyncEngine, table: str
) -> None:
    rows = await run(
        engine,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='f' "
        "AND conrelid=CAST(:t AS regclass) AND conname=:n",
        t=table,
        n=f"fk_{table}_promoted_approval_id",
    )
    assert rows and "REFERENCES pending_action(id)" in rows[0][0]


@pytest.mark.parametrize(
    "table",
    [
        "event",
        "llm_audit",
        "inbound_update",
        "memory_chunk",
        "graph_edge",
        "commitment",
        "decision",
        "pending_action",
    ],
)
async def test_truncate_is_forbidden_on_append_only_tables(engine: AsyncEngine, table: str) -> None:
    assert "TRUNCATE is forbidden" in await fails(engine, f"TRUNCATE {table} CASCADE")


async def test_low_hardening_checks(engine: AsyncEngine) -> None:
    ins = (
        "INSERT INTO pending_action (tool, action, payload, payload_sha256, nonce, expires_at) "
        "VALUES ('t','a','{}'::jsonb, :h, '\\x00'::bytea, now() + interval '5 minutes')"
    )
    assert "ck_pending_action_sha256" in await fails(engine, ins, h="A" * 64)  # uppercase
    assert "ck_pending_action_sha256" in await fails(engine, ins, h="g" * 64)
    eid = await mk_event(engine)
    cid = await mk_chunk(engine, eid)
    assert "ck_memory_chunk_not_self_superseded" in await fails(
        engine, "UPDATE memory_chunk SET superseded_by=id WHERE id=:i", i=cid
    )


async def test_lane_must_be_trusted_or_untrusted(engine: AsyncEngine) -> None:
    msg = await fails(
        engine,
        "INSERT INTO event (source_key, kind, source, lane, occurred_at) "
        "VALUES ('k-bad-lane', 'text', 's', 'maybe', now())",
    )
    assert "ck_event_lane" in msg


# ---- operational tables ---------------------------------------------------------------


async def test_inbound_update_replay_dedupe(engine: AsyncEngine) -> None:
    uid = int(uuid.uuid4().int % 10**12)
    sql = (
        "INSERT INTO inbound_update (update_id, kind) VALUES (:u, 'message') "
        "ON CONFLICT (update_id) DO NOTHING RETURNING id"
    )
    assert len(await run(engine, sql, u=uid)) == 1
    assert len(await run(engine, sql, u=uid)) == 0  # replay -> no row
    await fails(engine, "INSERT INTO inbound_update (update_id, kind) VALUES (:u, 'x')", u=uid)
    await run(engine, "UPDATE inbound_update SET processed_at=now() WHERE update_id=:u", u=uid)
    assert "INV-3" in await fails(
        engine, "UPDATE inbound_update SET update_id=update_id+1 WHERE update_id=:u", u=uid
    )


async def test_job_dedupe_key_unique(engine: AsyncEngine) -> None:
    key = f"reminder:{uuid.uuid4()}"
    ins = "INSERT INTO job (kind, run_at, dedupe_key) VALUES ('reminder', now(), :k)"
    await run(engine, ins, k=key)
    await fails(engine, ins, k=key)


async def test_job_skip_locked_claiming(engine: AsyncEngine) -> None:
    tag = uuid.uuid4().hex[:8]
    for i in range(2):
        await run(
            engine,
            "INSERT INTO job (kind, run_at, dedupe_key) "
            "VALUES (:k, now() - interval '1 minute', :d)",
            k=f"claimtest-{tag}",
            d=f"{tag}-{i}",
        )
    claim = (
        "SELECT id FROM job WHERE status='pending' AND run_at <= now() AND kind=:k "
        "ORDER BY run_at, dedupe_key FOR UPDATE SKIP LOCKED LIMIT 1"
    )
    async with engine.connect() as a, engine.connect() as b:
        ta, tb = await a.begin(), await b.begin()
        first = (await a.execute(text(claim), {"k": f"claimtest-{tag}"})).scalar_one()
        second = (await b.execute(text(claim), {"k": f"claimtest-{tag}"})).scalar_one()
        assert first != second  # B skipped A's locked row instead of blocking
        await tb.rollback()
        await ta.rollback()


async def test_pending_action_single_use_and_immutable(engine: AsyncEngine) -> None:
    aid = uuid.uuid4()
    await run(
        engine,
        "INSERT INTO pending_action (id, tool, action, payload, payload_sha256, nonce, "
        "expires_at) VALUES (:i, 'calendar', 'create', '{\"t\": 1}'::jsonb, :h, "
        "'\\x00'::bytea, now() + interval '10 minutes')",
        i=aid,
        h="a" * 64,
    )
    assert "INV-3" in await fails(
        engine, "UPDATE pending_action SET payload='{\"t\": 2}'::jsonb WHERE id=:i", i=aid
    )
    assert "INV-3" in await fails(engine, "DELETE FROM pending_action WHERE id=:i", i=aid)
    consume = (
        "UPDATE pending_action SET status='consumed', consumed_at=now() "
        "WHERE id=:i AND status='pending' RETURNING id"
    )
    assert len(await run(engine, consume, i=aid)) == 1
    assert len(await run(engine, consume, i=aid)) == 0  # atomic single use
    # even a direct UPDATE that bypasses the WHERE guard is refused by the trigger
    assert "single-use" in await fails(
        engine, "UPDATE pending_action SET status='cancelled' WHERE id=:i", i=aid
    )


async def test_pending_action_rejects_bad_hash_and_expiry(engine: AsyncEngine) -> None:
    base = (
        "INSERT INTO pending_action (tool, action, payload, payload_sha256, nonce, expires_at) "
        "VALUES ('t','a','{}'::jsonb, :h, '\\x00'::bytea, now() + :iv * interval '1 minute')"
    )
    assert "ck_pending_action_sha256" in await fails(engine, base, h="short", iv=10)
    assert "ck_pending_action_expiry" in await fails(engine, base, h="b" * 64, iv=-5)


async def test_system_flag_is_mutable_kv(engine: AsyncEngine) -> None:
    await run(
        engine,
        "INSERT INTO system_flag (key, value) VALUES ('kill_switch', '{\"on\": false}'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
    )
    await run(
        engine, "UPDATE system_flag SET value='{\"on\": true}'::jsonb WHERE key='kill_switch'"
    )
    rows = await run(engine, "SELECT value FROM system_flag WHERE key='kill_switch'")
    assert rows[0][0] == {"on": True}


# ---- migration behaviour ------------------------------------------------------------------


def test_downgrade_round_trip_preserves_pre_existing_rows() -> None:
    name = f"nimbus_schemart_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    dsn = _dsn_for(name)
    cfg = _cfg(dsn)

    async def sql(q: str) -> list[Any]:
        eng = create_async_engine(dsn, poolclass=NullPool)
        try:
            return await run(eng, q)
        finally:
            await eng.dispose()

    try:
        command.upgrade(cfg, PREV_REV)
        sid, nid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        asyncio.run(
            sql(
                f"INSERT INTO session (id,user_id,mode,started_at,device_id) VALUES "
                f"('{sid}','{uuid.uuid4()}','b',now(),'d')"
            )
        )
        asyncio.run(
            sql(
                f"INSERT INTO graph_node (id,entity_type,canonical_name,created_at) VALUES "
                f"('{nid}','p','x',now())"
            )
        )
        asyncio.run(
            sql(
                f"INSERT INTO graph_edge (id,subject_id,predicate,object_literal,valid_from,"
                f"ingested_at,source_session,confidence) VALUES "
                f"('{eid}','{nid}','p','o',now(),now(),'{sid}',0.5)"
            )
        )
        command.upgrade(cfg, "head")
        rows = asyncio.run(sql(f"SELECT lane, promoted FROM graph_edge WHERE id='{eid}'"))
        assert rows[0] == ("trusted", True)  # legacy rows get safe defaults
        command.downgrade(cfg, PREV_REV)
        left = asyncio.run(
            sql(
                "SELECT count(*) FROM pg_tables WHERE tablename IN "
                "('event','memory_chunk','job','llm_audit','system_flag','pending_action',"
                "'inbound_update')"
            )
        )
        assert left[0][0] == 0
        fns = asyncio.run(sql("SELECT count(*) FROM pg_proc WHERE proname LIKE 'nimbus\\_%'"))
        assert fns[0][0] == 0
        # legacy row survived the downgrade, and the legacy table has its old shape
        assert asyncio.run(sql(f"SELECT count(*) FROM graph_edge WHERE id='{eid}'"))[0][0] == 1
        cols = asyncio.run(
            sql(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='graph_edge' AND column_name IN ('lane','promoted')"
            )
        )
        assert cols == []
        command.upgrade(cfg, "head")
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
