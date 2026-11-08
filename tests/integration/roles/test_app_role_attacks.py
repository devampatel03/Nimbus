# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P6.1 reviewer findings (ADR-0026): two plain-SQL attacks an app-role process
could use to forge the approval that R8 / INV-5 guards trust.

1. TEMP SHADOW: create a temporary `pending_action` table holding a forged
   consumed row with the id of a REAL pending approval. The guard functions read
   `pending_action` unqualified, so pg_temp would win the lookup. Closed twice:
   TEMPORARY is revoked (denial asserted), and the functions are pinned to
   `<schema>, pg_temp` (asserted by granting TEMP back, as the owner).
2. BORN CONSUMED: INSERT a ready-made `consumed` approval. Closed by the
   BEFORE INSERT trigger `nimbus_pending_birth_guard`.

Each fix is mutation-proved: undo it as the owner and the attack must succeed
(so the test can fail); the fix is restored in `finally`.

Residual, stated plainly: the app role legitimately INSERTs a pending approval
and UPDATEs it to consumed (that is how a user's tap is recorded). The database
cannot tell a genuine tap from a compromised app doing the same; that is bounded
by the token/TOTP checks in app code, not by this role."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests._memory_helpers import make_event as contract_event
from tests._memory_helpers import store_for
from tests.adversarial.memory.test_poisoning_controls import two_chunks, untrusted_edges
from tests.integration.roles.conftest import RoleDb

pytestmark = pytest.mark.asyncio

GUARDS = (
    "nimbus_append_only()",
    "nimbus_single_use_guard()",
    "nimbus_no_truncate()",
    "nimbus_promotion_guard()",
    "nimbus_ledger_guard()",
    "nimbus_pending_birth_guard()",
)
SHADOW = (
    "CREATE TEMP TABLE pending_action (id uuid, status text, consumed_at timestamptz, "
    "expires_at timestamptz, tool text, action text, payload jsonb) ON COMMIT DROP"
)
SHADOW_ROW = (
    "INSERT INTO pending_action VALUES (:i, 'consumed', now(), now() + interval '1 hour', "
    ":t, :a, CAST(:p AS jsonb))"
)
PIN = (
    "DO $p$ BEGIN EXECUTE format('ALTER FUNCTION {g} SET search_path = %I, pg_temp', "
    "current_schema()); END $p$"
)


async def owner_sql(db: RoleDb, *stmts: str) -> None:
    async with db.owner.begin() as conn:
        for s in stmts:
            await conn.execute(text(s))


async def real_pending(db: RoleDb, tool: str, action: str, payload: dict[str, str]) -> uuid.UUID:
    aid = uuid.uuid4()
    async with db.owner_sf() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO pending_action (id, tool, action, payload, payload_sha256, nonce, "
                "status, expires_at, created_at) VALUES (:i, :t, :a, CAST(:p AS jsonb), :h, :n, "
                "'pending', now() + interval '1 hour', now())"
            ),
            {
                "i": aid,
                "t": tool,
                "a": action,
                "p": json.dumps(payload),
                "h": hashlib.sha256(aid.bytes).hexdigest(),
                "n": b"n" * 16,
            },
        )
    return aid


@asynccontextmanager
async def temp_granted(db: RoleDb, *, unpinned: bool = False) -> AsyncIterator[None]:
    """Owner re-grants TEMP to the app role (undoing fix 1); optionally also
    removes the search_path pins (undoing fix 2). Always restored."""
    await owner_sql(db, f'GRANT TEMPORARY ON DATABASE "{db.name}" TO "{db.role}"')
    if unpinned:
        await owner_sql(db, *(f"ALTER FUNCTION {g} RESET search_path" for g in GUARDS))
    try:
        yield
    finally:
        await owner_sql(db, f'REVOKE TEMPORARY ON DATABASE "{db.name}" FROM "{db.role}"')
        if unpinned:
            await owner_sql(db, *(PIN.format(g=g) for g in GUARDS))


async def shadow_promote(db: RoleDb, table: str) -> None:
    """Seed (owner) an untrusted item + a REAL pending approval; then, as the app
    role, forge the consumed approval in a temp shadow and promote."""
    async with db.owner_sf() as s, s.begin():
        if table == "memory_chunk":
            _, item, _ = await two_chunks(s)
        else:
            _, edges, _ = await untrusted_edges(s, 1)
            item = edges[0].id
    aid = await real_pending(db, "memory", "promote", {"item_id": str(item)})
    async with db.app.connect() as conn, conn.begin():
        await conn.execute(text(SHADOW))
        await conn.execute(
            text(SHADOW_ROW),
            {"i": aid, "t": "memory", "a": "promote", "p": json.dumps({"item_id": str(item)})},
        )
        await conn.execute(
            text(f"UPDATE {table} SET promoted = true, promoted_approval_id = :a WHERE id = :i"),
            {"a": aid, "i": item},
        )


LEDGER_INS = (
    "INSERT INTO commitment_ledger (id, commitment_key, version, supersedes_id, status, "
    "description, norm_key, dedupe_key, source_event_id, lane, confidence, promoted, "
    "promoted_approval_id) VALUES (:id, :ck, :v, :sup, :st, :d, 'n', :dk, :ev, 'trusted', "
    "0.9, :pr, :ap)"
)


async def shadow_confirm_ledger(db: RoleDb) -> None:
    key, cand = uuid.uuid4(), uuid.uuid4()
    async with db.owner_sf() as s, s.begin():
        src = f"telegram:{uuid.uuid4().int % 10**9}"
        ev = await store_for(s).record_event(contract_event(src))
    aid = await real_pending(db, "commitment", "confirm", {"candidate_id": str(cand)})
    base = {"ck": key, "ev": ev, "d": "send the deck", "t": datetime.now(UTC)}
    async with db.owner_sf() as s, s.begin():
        await s.execute(
            text(LEDGER_INS),
            {
                **base,
                "id": cand,
                "v": 1,
                "sup": None,
                "st": "candidate",
                "dk": f"k:{cand}",
                "pr": False,
                "ap": None,
            },
        )
    async with db.app.connect() as conn, conn.begin():
        await conn.execute(text(SHADOW))
        await conn.execute(
            text(SHADOW_ROW),
            {
                "i": aid,
                "t": "commitment",
                "a": "confirm",
                "p": json.dumps({"candidate_id": str(cand)}),
            },
        )
        await conn.execute(
            text(LEDGER_INS),
            {
                **base,
                "id": uuid.uuid4(),
                "v": 2,
                "sup": cand,
                "st": "confirmed",
                "dk": f"k:{uuid.uuid4()}",
                "pr": True,
                "ap": aid,
            },
        )


# --------------------------------------------------------------- attack 1


async def test_temp_tables_are_denied_to_the_app_role(roledb: RoleDb) -> None:
    async with roledb.app.connect() as conn:
        with pytest.raises(DBAPIError) as ei:
            await conn.execute(text(SHADOW))
    assert getattr(ei.value.orig, "sqlstate", None) == "42501"


async def test_guard_functions_are_pinned_with_pg_temp_last(roledb: RoleDb) -> None:
    async with roledb.owner.connect() as conn:
        for g in GUARDS:
            cfg = (
                await conn.execute(
                    text("SELECT proconfig FROM pg_proc WHERE oid = CAST(:g AS regprocedure)"),
                    {"g": g},
                )
            ).scalar_one()
            assert cfg and any(
                c.startswith("search_path=") and c.endswith(", pg_temp") for c in cfg
            ), (g, cfg)


@pytest.mark.parametrize("table", ["memory_chunk", "graph_edge"])
async def test_shadow_attack_blocked_by_pin_even_if_temp_were_granted(
    roledb: RoleDb, table: str
) -> None:
    async with temp_granted(roledb):
        with pytest.raises(DBAPIError) as ei:
            await shadow_promote(roledb, table)
    assert "(R8)" in str(ei.value.orig)


async def test_shadow_attack_on_ledger_blocked_by_pin(roledb: RoleDb) -> None:
    async with temp_granted(roledb):
        with pytest.raises(DBAPIError) as ei:
            await shadow_confirm_ledger(roledb)
    assert "no consumed approval" in str(ei.value.orig)


@pytest.mark.parametrize("table", ["memory_chunk", "graph_edge"])
async def test_mutation_without_fixes_the_shadow_attack_succeeds(
    roledb: RoleDb, table: str
) -> None:
    async with temp_granted(roledb, unpinned=True):
        await shadow_promote(roledb, table)  # must NOT raise: the test can fail


async def test_mutation_without_fixes_the_ledger_shadow_attack_succeeds(roledb: RoleDb) -> None:
    async with temp_granted(roledb, unpinned=True):
        await shadow_confirm_ledger(roledb)


# --------------------------------------------------------------- attack 2

BORN = (
    "INSERT INTO pending_action (id, tool, action, payload, payload_sha256, nonce, status, "
    "expires_at, created_at, consumed_at) VALUES (:i, 'memory', 'promote', '{}'::jsonb, :h, :n, "
    "'__S__', now() + interval '1 hour', now(), __C__)"
)


async def born(db: RoleDb, status: str = "consumed", consumed: str = "now()") -> None:
    sql = BORN.replace("__S__", status).replace("__C__", consumed)
    async with db.app.begin() as conn:
        await conn.execute(text(sql), {"i": uuid.uuid4(), "h": "a" * 64, "n": b"n" * 16})


async def test_born_consumed_approval_is_refused(roledb: RoleDb) -> None:
    with pytest.raises(DBAPIError) as ei:
        await born(roledb)
    assert "created pending" in str(ei.value.orig)


async def test_born_rejected_and_pre_stamped_approvals_are_refused(roledb: RoleDb) -> None:
    for status, consumed in (("rejected", "NULL"), ("pending", "now()")):
        with pytest.raises(DBAPIError):
            await born(roledb, status, consumed)


async def test_mutation_without_birth_trigger_a_born_consumed_row_is_accepted(
    roledb: RoleDb,
) -> None:
    await owner_sql(roledb, "ALTER TABLE pending_action DISABLE TRIGGER trg_pending_action_birth")
    try:
        await born(roledb)  # must NOT raise: the test can fail
    finally:
        await owner_sql(
            roledb, "ALTER TABLE pending_action ENABLE TRIGGER trg_pending_action_birth"
        )
    with pytest.raises(DBAPIError):
        await born(roledb)


async def test_normal_pending_to_consumed_path_still_works_for_the_app_role(
    roledb: RoleDb,
) -> None:
    aid = uuid.uuid4()
    async with roledb.app.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pending_action (id, tool, action, payload, payload_sha256, nonce, "
                "expires_at) VALUES (:i, 'tasks', 'call', '{}'::jsonb, :h, :n, "
                "now() + interval '1 hour')"
            ),
            {"i": aid, "h": "c" * 64, "n": b"n" * 16},
        )
        got = await conn.execute(
            text(
                "UPDATE pending_action SET status='consumed', consumed_at=now() "
                "WHERE id=:i AND status='pending' RETURNING id"
            ),
            {"i": aid},
        )
        assert got.scalar_one() == aid
