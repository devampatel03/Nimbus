# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P6.1 / ADR-0026 (INV-3, R7, R8): the least-privilege app role CANNOT switch
off the database guards. Real Postgres. Every denial is asserted as SQLSTATE
42501 (insufficient_privilege) from the privilege system itself, NOT from the
triggers: the point is that the triggers cannot be removed."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.roles import DELETABLE, role_report
from src.memory.schemas import Base
from src.memory.schemas.triggers import APPEND_ONLY_TABLES, PROMOTION_GUARDED_TABLES
from tests.integration.roles.conftest import RoleDb

pytestmark = pytest.mark.asyncio

FULLY_IMMUTABLE = sorted(t for t, (mut, _) in APPEND_ONLY_TABLES.items() if not mut)
TRIGGERED = sorted(set(APPEND_ONLY_TABLES) | set(PROMOTION_GUARDED_TABLES))
TRIGGER_Q = (
    "SELECT tgname, CAST(tgenabled AS text) FROM pg_trigger "
    "WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"
)


async def denied(db: RoleDb, sql: str) -> None:
    """Run `sql` as the app role; it must fail with SQLSTATE 42501."""
    async with db.app.connect() as conn:
        try:
            await conn.execute(text(sql))
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            assert code == "42501", (sql, code, str(exc.orig)[:160])
            return
    pytest.fail(f"app role was allowed to run: {sql}")


async def test_role_is_not_superuser_not_owner(roledb: RoleDb) -> None:
    async with roledb.app.connect() as conn:
        rep = await role_report(conn)
        attrs = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    assert rep.least_privilege and rep.user == roledb.role
    assert not any(attrs)
    async with roledb.owner.connect() as conn:
        assert (await role_report(conn)).least_privilege is False  # control: owner is flagged


@pytest.mark.parametrize("table", TRIGGERED)
async def test_cannot_disable_or_drop_triggers(roledb: RoleDb, table: str) -> None:
    await denied(roledb, f'ALTER TABLE "{table}" DISABLE TRIGGER ALL')
    await denied(roledb, f'ALTER TABLE "{table}" DISABLE TRIGGER USER')
    async with roledb.owner.connect() as conn:
        rows = (await conn.execute(text(TRIGGER_Q), {"t": table})).all()
    assert rows, table
    for name, _ in rows:
        await denied(roledb, f'DROP TRIGGER "{name}" ON "{table}"')
        await denied(roledb, f'ALTER TABLE "{table}" DISABLE TRIGGER "{name}"')
    async with roledb.owner.connect() as conn:
        after = (await conn.execute(text(TRIGGER_Q), {"t": table})).all()
    assert {r[1] for r in after} == {"O"} and len(after) == len(rows)  # present and enabled


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
async def test_no_truncate_trigger_or_ddl_privilege(roledb: RoleDb, table: str) -> None:
    await denied(roledb, f'TRUNCATE "{table}"')
    await denied(roledb, f'ALTER TABLE "{table}" ADD COLUMN x int')
    await denied(roledb, f'DROP TABLE "{table}"')
    async with roledb.owner.connect() as conn:
        got = (
            await conn.execute(
                text(
                    "SELECT has_table_privilege(CAST(:r AS text), CAST(:t AS text), 'TRUNCATE'), "
                    "has_table_privilege(CAST(:r AS text), CAST(:t AS text), 'TRIGGER'), "
                    "has_table_privilege(CAST(:r AS text), CAST(:t AS text), 'REFERENCES')"
                ),
                {"r": roledb.role, "t": table},
            )
        ).one()
    assert tuple(got) == (False, False, False)


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
async def test_no_delete_on_append_only_tables(roledb: RoleDb, table: str) -> None:
    await denied(roledb, f'DELETE FROM "{table}"')


@pytest.mark.parametrize("table", FULLY_IMMUTABLE)
async def test_fully_immutable_tables_have_no_update_at_all(roledb: RoleDb, table: str) -> None:
    col = list(Base.metadata.tables[table].columns)[-1].name
    await denied(roledb, f'UPDATE "{table}" SET "{col}" = "{col}"')


async def test_update_is_limited_to_the_lifecycle_columns(roledb: RoleDb) -> None:
    async with roledb.owner.connect() as conn:
        for table, (mutable, _) in APPEND_ONLY_TABLES.items():
            for col in Base.metadata.tables[table].columns:
                ok = (
                    await conn.execute(
                        text(
                            "SELECT has_column_privilege(CAST(:r AS text), CAST(:t AS text), "
                            "CAST(:c AS text), 'UPDATE')"
                        ),
                        {"r": roledb.role, "t": table, "c": col.name},
                    )
                ).scalar_one()
                assert ok is (col.name in mutable), (table, col.name)
    # Protected columns, attempted for real.
    await denied(roledb, "UPDATE event SET body = 'x'")
    await denied(roledb, "UPDATE memory_chunk SET content = 'x'")
    await denied(roledb, "UPDATE graph_edge SET valid_from = now()")
    await denied(roledb, "UPDATE pending_action SET payload = '{}'::jsonb")
    await denied(roledb, "UPDATE commitment_ledger SET status = 'confirmed'")


async def test_non_append_only_deletes_are_exactly_the_audited_list(roledb: RoleDb) -> None:
    async with roledb.owner.connect() as conn:
        for table in Base.metadata.tables:
            if table in APPEND_ONLY_TABLES:
                continue
            can = (
                await conn.execute(
                    text(
                        "SELECT has_table_privilege(CAST(:r AS text), CAST(:t AS text), 'DELETE')"
                    ),
                    {"r": roledb.role, "t": table},
                )
            ).scalar_one()
            assert can is (table in DELETABLE), table


async def test_cannot_bypass_triggers_or_extend_the_schema(roledb: RoleDb) -> None:
    await denied(roledb, "SET session_replication_role = replica")
    await denied(roledb, "SET session_replication_role = 'local'")
    await denied(roledb, "CREATE EXTENSION pg_trgm")  # trusted ext: needs CREATE on the DB
    await denied(roledb, "CREATE EXTENSION file_fdw")  # untrusted ext: needs superuser
    await denied(roledb, "CREATE TABLE evil (x int)")
    await denied(roledb, "CREATE SCHEMA evil")
    await denied(
        roledb,
        "CREATE OR REPLACE FUNCTION nimbus_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $f$ BEGIN RETURN NEW; END $f$",
    )
    await denied(roledb, "DROP FUNCTION nimbus_append_only() CASCADE")
    await denied(roledb, "ALTER ROLE current_user SUPERUSER")
    await denied(roledb, "CREATE ROLE evil SUPERUSER")
    await denied(roledb, "COPY event TO PROGRAM 'true'")


async def test_every_orm_table_is_a_deliberate_choice(roledb: RoleDb) -> None:
    """A new table must be added to APPEND_ONLY_TABLES, DELETABLE or this list,
    so a reviewer decides its privileges (default: no DELETE)."""
    plain_no_delete = {
        "person",
        "session",
        "agent_stance",
        "user_trait",
        "trait_evidence",
        "graph_node",
    }
    known = set(APPEND_ONLY_TABLES) | set(DELETABLE) | plain_no_delete
    assert set(Base.metadata.tables) <= known, sorted(set(Base.metadata.tables) - known)
