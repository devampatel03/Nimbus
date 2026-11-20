# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""promotion guard on memory_chunk and graph_edge (P4.6; R8, R7, INV-3).

`nimbus_promotion_guard()` (frozen copy of src/memory/schemas/triggers.py), BEFORE
INSERT OR UPDATE on both tables:

* an untrusted row cannot be inserted promoted;
* an untrusted row becomes promoted only with a consumed, in-time `memory`/`promote`
  pending_action whose payload `item_id` is exactly this row's id (so a forged
  or reused approval id, or an approval for another item, is refused by the DB);
* a row cannot be demoted (promoted true -> false); retract by invalidating;
* a row cannot be inserted `trusted` when its source event is not (lane spoofing).

Existing rows are not re-checked (the guard fires on new writes). Downgrade drops
the triggers and the function; no data is touched.

Revision ID: e5b1c7a3d902
Revises: a9d4c6e2f801
Create Date: 2026-10-30

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5b1c7a3d902"  # pragma: allowlist secret
down_revision: str | None = "a9d4c6e2f801"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("memory_chunk", "graph_edge")

PROMOTION_GUARD_FN_SQL = """
CREATE OR REPLACE FUNCTION nimbus_promotion_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    -- Lane spoofing: a derived row may not be more trusted than its source event.
    IF TG_OP = 'INSERT' AND NEW.lane = 'trusted' AND NEW.source_event_id IS NOT NULL
       AND EXISTS (SELECT 1 FROM event e WHERE e.id = NEW.source_event_id AND e.lane <> 'trusted')
    THEN
        RAISE EXCEPTION '%: row lane is more trusted than its source event (R8)', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.promoted AND NOT NEW.promoted THEN
        RAISE EXCEPTION '%: demotion is forbidden; invalidate instead (INV-3)', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.lane = 'untrusted' AND NEW.promoted THEN
        IF TG_OP = 'UPDATE' AND OLD.promoted THEN
            RETURN NEW;  -- already promoted; the append-only trigger freezes the approval id
        END IF;
        IF TG_OP = 'INSERT' THEN
            RAISE EXCEPTION '%: an untrusted row cannot be born promoted (R8)', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pending_action p
            WHERE p.id = NEW.promoted_approval_id AND p.status = 'consumed'
              AND p.consumed_at IS NOT NULL AND p.consumed_at <= p.expires_at
              AND p.tool = 'memory' AND p.action = 'promote'
              AND p.payload ->> 'item_id' = NEW.id::text
        ) THEN
            RAISE EXCEPTION '%: promotion needs a consumed approval for this exact item (R8)',
                TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$
"""


def upgrade() -> None:
    op.execute(PROMOTION_GUARD_FN_SQL)
    for t in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{t}_promotion_guard BEFORE INSERT OR UPDATE ON {t} "
            "FOR EACH ROW EXECUTE FUNCTION nimbus_promotion_guard()"
        )


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_promotion_guard ON {t}")
    op.execute("DROP FUNCTION IF EXISTS nimbus_promotion_guard()")
