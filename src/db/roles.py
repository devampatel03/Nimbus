# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Least-privilege runtime role (P6.1, ADR-0026; closes the superuser HIGH).

Two Postgres roles:

* the OWNER (`POSTGRES_USER`, today `nimbus`, a superuser in the container): runs
  migrations and `db-bootstrap`, owns every table, function and trigger.
* the APP role (`nimbus_app`): what the running bot connects as. Not a
  superuser, not an owner, no CREATE, no TRUNCATE, no TRIGGER, no DELETE on
  append-only tables, and column-level UPDATE only on the lifecycle columns the
  append-only triggers already allow. Disabling or dropping a trigger needs
  table ownership; `session_replication_role` and `CREATE EXTENSION vector`
  need superuser. So a compromised or buggy app process cannot switch off
  INV-3 / R7 / R8, whatever SQL it manages to issue.

`apply_app_grants` is idempotent and is re-run after every migration (new
tables get NO privileges until then: default-deny). Role creation is in
`create_role`, also idempotent; the compose init script does the same on a
fresh volume.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.memory.schemas.triggers import APPEND_ONLY_TABLES

log = logging.getLogger(__name__)

DEFAULT_APP_ROLE = "nimbus_app"
_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Tables the app may DELETE from (everything else: never). Verified by grep over
# src/: ingest attempt counters and Google credentials (system_flag), the job
# queue, reminders and tasks.
DELETABLE: frozenset[str] = frozenset({"system_flag", "job", "reminder", "task"})
# Never granted to the app at all (migration bookkeeping).
HIDDEN: frozenset[str] = frozenset({"alembic_version"})


def _ident(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValueError("invalid role name")
    return f'"{name}"'


@dataclass(frozen=True)
class RoleReport:
    user: str
    superuser: bool
    owns_tables: bool

    @property
    def least_privilege(self) -> bool:
        return not self.superuser and not self.owns_tables

    def describe(self) -> str:
        if self.least_privilege:
            return f"db role={self.user} least-privilege (non-superuser, non-owner)"
        return f"db role={self.user} OWNER/SUPERUSER privileges"


async def role_report(conn: AsyncConnection) -> RoleReport:
    row = (
        await conn.execute(
            text(
                "SELECT current_user::text, "
                "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                "EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' "
                "AND tableowner = current_user)"
            )
        )
    ).one()
    return RoleReport(str(row[0]), bool(row[1]), bool(row[2]))


async def log_role_report(engine: AsyncEngine, *, expect_split: bool) -> RoleReport | None:
    """Startup health line. WARNING when the app holds owner privileges."""
    try:
        async with engine.connect() as conn:
            rep = await role_report(conn)
    except Exception as exc:  # noqa: BLE001 - never block boot on a report
        log.warning("db role report unavailable err=%s", type(exc).__name__)
        return None
    if rep.least_privilege:
        log.info(rep.describe())
    else:
        reason = "POSTGRES_APP_USER/PASSWORD not set" if not expect_split else "role is privileged"
        log.warning(
            "%s: the app runs with owner privileges (%s); append-only triggers can be "
            "disabled by this process. Configure a separate runtime DSN (ADR-0026).",
            rep.describe(),
            reason,
        )
    return rep


async def create_role(conn: AsyncConnection, role: str, password: str | None) -> None:
    """Create or normalise the app role (needs CREATEROLE: the owner has it).
    The password is quoted server-side with format(%L); it never appears in a
    logged parameter, and a failure is re-raised without the statement text."""
    ident = _ident(role)
    exists = (
        await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
    ).first()
    attrs = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
    await conn.execute(text(f"{'ALTER' if exists else 'CREATE'} ROLE {ident} {attrs}"))
    if password is not None:
        got = await conn.execute(
            text("SELECT format('ALTER ROLE %I PASSWORD %L', CAST(:r AS text), CAST(:p AS text))"),
            {"r": role, "p": password},
        )
        stmt: str = got.scalar_one()
        try:
            await conn.exec_driver_sql(stmt)
        except Exception as exc:  # noqa: BLE001 - the statement text carries the password
            raise RuntimeError(f"could not set role password ({type(exc).__name__})") from None


def _update_columns(table: str) -> tuple[str, ...] | None:
    """None = full-row UPDATE, () = no UPDATE, else the allowed columns."""
    if table in APPEND_ONLY_TABLES:
        return APPEND_ONLY_TABLES[table][0]
    return None


async def apply_app_grants(conn: AsyncConnection, role: str, *, database: str) -> None:
    """Idempotently set the app role's privileges (default-deny, then exact
    grants). A table outside every explicit list gets SELECT/INSERT/UPDATE, no
    DELETE; `tests/integration/roles` pins that every ORM table is deliberate."""
    r = _ident(role)
    db = _ident(database)
    await conn.execute(text(f"REVOKE ALL ON DATABASE {db} FROM {r}"))
    await conn.execute(text(f"GRANT CONNECT ON DATABASE {db} TO {r}"))
    await conn.execute(text(f"REVOKE ALL ON SCHEMA public FROM {r}"))
    await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {r}"))
    await conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {r}"))
    await conn.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {r}"))
    res = await conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
    )
    tables: list[str] = [str(row[0]) for row in res.all()]
    for t in tables:
        if t in HIDDEN:
            continue
        q = f'"{t}"'
        await conn.execute(text(f"GRANT SELECT, INSERT ON {q} TO {r}"))
        cols = _update_columns(t)
        if cols is None:
            await conn.execute(text(f"GRANT UPDATE ON {q} TO {r}"))
            if t in DELETABLE:
                await conn.execute(text(f"GRANT DELETE ON {q} TO {r}"))
        elif cols:
            col_sql = ", ".join(f'"{c}"' for c in cols)
            await conn.execute(text(f"GRANT UPDATE ({col_sql}) ON {q} TO {r}"))
    await conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}"))
