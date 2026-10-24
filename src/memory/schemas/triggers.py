# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Append-only enforcement (INV-3 / R7) as Postgres triggers.

One generic trigger function, `nimbus_append_only()`, parametrised per table
through trigger arguments:

  TG_ARGV[0]  comma list of columns that MAY change on UPDATE (supersession /
              lifecycle columns, e.g. valid_to, superseded_by, promoted).
  TG_ARGV[1]  comma list of "set-once" columns: may change only while the old
              value is NULL (e.g. valid_to: NULL -> value, never rewritten).

Any other column change raises; DELETE always raises. The comparison is done
on `to_jsonb(row)` so the function is schema-agnostic and cannot drift when a
column is added (a new column is immutable by default).

`pending_action` additionally gets `nimbus_single_use_guard()`: once a row
has left status 'pending' it can never change again (single-use approvals).

This module is the ORM-side (create_all) copy; the Alembic revision carries a
frozen copy of the same SQL. tests/integration/test_schema_triggers.py checks
the migrated schema behaves as specified.
"""

from sqlalchemy import DDL, event
from sqlalchemy.schema import MetaData, Table

APPEND_ONLY_FN_SQL = """
CREATE OR REPLACE FUNCTION nimbus_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    mutable text[] := string_to_array(coalesce(TG_ARGV[0], ''), ',');
    once    text[] := string_to_array(coalesce(TG_ARGV[1], ''), ',');
    col     text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'append-only table %: DELETE is forbidden (INV-3)', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (to_jsonb(NEW) - mutable - once) IS DISTINCT FROM (to_jsonb(OLD) - mutable - once) THEN
        RAISE EXCEPTION 'append-only table %: immutable column changed (INV-3)', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    FOREACH col IN ARRAY once LOOP
        IF col <> '' AND (to_jsonb(OLD) -> col) <> 'null'::jsonb
           AND (to_jsonb(NEW) -> col) IS DISTINCT FROM (to_jsonb(OLD) -> col) THEN
            RAISE EXCEPTION 'append-only table %: set-once column % rewritten (INV-3)',
                TG_TABLE_NAME, col
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END
$fn$
"""

SINGLE_USE_FN_SQL = """
CREATE OR REPLACE FUNCTION nimbus_single_use_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'pending_action % is already %; single-use', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$
"""

NO_TRUNCATE_FN_SQL = """
CREATE OR REPLACE FUNCTION nimbus_no_truncate() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'append-only table %: TRUNCATE is forbidden (INV-3)', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END
$fn$
"""

# table -> (mutable columns, set-once columns)
APPEND_ONLY_TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "event": ((), ()),
    "llm_audit": (("payload_redacted_enc",), ()),  # 14-day retention purge nulls it
    "inbound_update": (("processed_at", "event_id"), ("processed_at", "event_id")),
    "memory_chunk": (
        ("invalidated_at", "superseded_by", "promoted", "promoted_approval_id"),
        ("invalidated_at", "superseded_by", "promoted_approval_id"),
    ),
    "graph_edge": (
        (
            "valid_to",
            "invalidated_at",
            "superseded_by",
            "user_verified",
            "promoted",
            "promoted_approval_id",
        ),
        ("valid_to", "invalidated_at", "superseded_by", "promoted_approval_id"),
    ),
    "commitment": (
        ("status", "closed_at", "closure_source_session", "promoted", "promoted_approval_id"),
        ("closed_at", "closure_source_session", "promoted_approval_id"),
    ),
    "decision": (
        ("outcome", "outcome_captured_at", "promoted", "promoted_approval_id"),
        ("outcome", "outcome_captured_at", "promoted_approval_id"),
    ),
    "pending_action": (("status", "consumed_at"), ("consumed_at",)),
}


def append_only_trigger_name(table: str) -> str:
    return f"trg_{table}_append_only"


def append_only_trigger_sql(table: str) -> str:
    mutable, once = APPEND_ONLY_TABLES[table]
    return (
        f"CREATE TRIGGER {append_only_trigger_name(table)} BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION "
        f"nimbus_append_only('{','.join(mutable)}','{','.join(once)}')"
    )


SINGLE_USE_TRIGGER_SQL = (
    "CREATE TRIGGER trg_pending_action_single_use BEFORE UPDATE ON pending_action "
    "FOR EACH ROW EXECUTE FUNCTION nimbus_single_use_guard()"
)


def truncate_guard_sql(table: str) -> str:
    return (
        f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION nimbus_no_truncate()"
    )


def _ddl(sql: str) -> DDL:
    # DDL() applies %-formatting; literal percent signs must be doubled.
    return DDL(sql.replace("%", "%%")).execute_if(  # type: ignore[no-untyped-call]
        dialect="postgresql"
    )


def install_orm_ddl(metadata: MetaData) -> None:
    """Make `metadata.create_all()` (the pytest path) create the extension,
    functions and triggers too, so ORM-created and migrated schemas agree."""
    event.listen(metadata, "before_create", _ddl("CREATE EXTENSION IF NOT EXISTS vector"))
    event.listen(metadata, "before_create", _ddl(APPEND_ONLY_FN_SQL))
    event.listen(metadata, "before_create", _ddl(SINGLE_USE_FN_SQL))
    event.listen(metadata, "before_create", _ddl(NO_TRUNCATE_FN_SQL))


def attach_table_triggers(table: Table) -> None:
    if table.name in APPEND_ONLY_TABLES:
        event.listen(table, "after_create", _ddl(append_only_trigger_sql(table.name)))
        event.listen(table, "after_create", _ddl(truncate_guard_sql(table.name)))
    if table.name == "pending_action":
        event.listen(table, "after_create", _ddl(SINGLE_USE_TRIGGER_SQL))
