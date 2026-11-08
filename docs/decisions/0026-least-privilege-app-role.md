# ADR-0026: Separate owner (migration) and least-privilege app database roles (P6.1)

Status: Proposed (reviewer sign-off)
Date: 2026-11-08
Satisfies: INV-3, R7, R8. Closes the HIGH in CLAUDE.md "Known violations" once configured.

## Context

The app connected as `nimbus`, the bootstrap superuser. A superuser can
`ALTER TABLE .. DISABLE TRIGGER`, `SET session_replication_role = replica`,
TRUNCATE and DROP, so a compromised or buggy process could turn off every
append-only, single-use and promotion guard. The guards were only as strong as
the DB role.

## Decision

1. Two roles. The OWNER (`POSTGRES_USER`) runs `alembic upgrade head` and
   `python -m src.app db-bootstrap`, and owns tables, functions and triggers.
   The APP role (`POSTGRES_APP_USER`, default `nimbus_app`) is what the running
   bot uses: LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOINHERIT, no CREATE
   on schema or database. Disabling or dropping triggers needs ownership;
   `session_replication_role` and untrusted extensions need superuser.
2. Grants (`src/db/roles.py`, re-applied idempotently after every migration;
   default-deny, no `ALTER DEFAULT PRIVILEGES`, so a new table has NO app access
   until reviewed): SELECT, INSERT everywhere; UPDATE on append-only tables only
   on the lifecycle columns the triggers already allow (`APPEND_ONLY_TABLES`),
   none on `event`, `policy_audit`, `commitment_ledger`; full UPDATE on other
   tables; DELETE only on `system_flag`, `job`, `reminder`, `task` (grep of
   `src/`); never TRUNCATE, TRIGGER, REFERENCES. `alembic_version` is hidden.
3. Settings: `POSTGRES_APP_USER` + `POSTGRES_APP_PASSWORD`. Unset = old
   behaviour (one DSN, owner privileges) plus a startup WARNING. Set = runtime
   uses `Settings.runtime_dsn`; Alembic and `db-bootstrap` keep `postgres_dsn`.
   Startup logs `db role=... least-privilege` or the WARNING (the health report).
4. The long-running container's env file contains no owner credentials; the
   owner password lives in a separate file used by the one-shot `migrate` service.

## Consequences

- Residual: a process that can read `/etc/nimbus/migrate.env` (root on the VM,
  or anyone in the docker group) is the owner. This bounds the app process, not
  the host admin. Postgres superuser inside the container is still `nimbus`.
- The role can still INSERT and use the allowed UPDATE columns; the triggers
  keep constraining those (control test in `tests/integration/roles`).
- Tests: `tests/integration/roles` (denials asserted as SQLSTATE 42501, plus the
  normal app flows under the role). They run in CI against the service Postgres.
- Not verified on a VM or against a long-lived production volume.
