# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""guard hardening for the least-privilege app role (P6.1, ADR-0026).

1. Pin `search_path = <schema>, pg_temp` (pg_temp LAST) on every guard trigger
   function. They read tables unqualified (`pending_action`, `event`,
   `commitment_ledger`); without a pin a role with TEMP privilege could shadow
   them with a same-named temporary table and forge the approval the guards trust.
2. `nimbus_pending_birth_guard()`: a pending_action must be INSERTed pending and
   unconsumed. Consumption stays an UPDATE governed by `nimbus_single_use_guard`,
   so a ready-made 'consumed' approval can no longer be inserted.

Downgrade drops the birth trigger/function and resets the search_path pins.
No data is touched; existing rows are not re-checked.

Revision ID: b8d3f2a61c94
Revises: b2f6d4a8c1e7
Create Date: 2026-11-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8d3f2a61c94"  # pragma: allowlist secret
down_revision: str | None = "b2f6d4a8c1e7"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (do not import from src: a migration must not change under you).
EXISTING = (
    "nimbus_append_only()",
    "nimbus_single_use_guard()",
    "nimbus_no_truncate()",
    "nimbus_promotion_guard()",
    "nimbus_ledger_guard()",
)
BIRTH_FN = """
CREATE OR REPLACE FUNCTION nimbus_pending_birth_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.status <> 'pending' OR NEW.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'pending_action must be created pending and unconsumed (INV-5, R8)'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$
"""


def _pin(fn: str) -> str:
    return (
        "DO $pin$ BEGIN EXECUTE format("
        f"'ALTER FUNCTION {fn} SET search_path = %I, pg_temp', current_schema()); END $pin$"
    )


def upgrade() -> None:
    op.execute(BIRTH_FN)
    for fn in (*EXISTING, "nimbus_pending_birth_guard()"):
        op.execute(_pin(fn))
    op.execute(
        "CREATE TRIGGER trg_pending_action_birth BEFORE INSERT ON pending_action "
        "FOR EACH ROW EXECUTE FUNCTION nimbus_pending_birth_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_pending_action_birth ON pending_action")
    op.execute("DROP FUNCTION IF EXISTS nimbus_pending_birth_guard()")
    for fn in EXISTING:
        op.execute(f"ALTER FUNCTION {fn} RESET search_path")
