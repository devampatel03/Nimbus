# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""INV-3: Graph facts are never mutated in place — supersession only.

Acceptance: `src/memory/graph.py` (GraphRepository, the only code path
that writes to `graph_edge`) must never:
  - assign to `GraphEdge.object_id`, `.object_literal` or `.valid_from`
    after construction, or
  - issue a `sqlalchemy.update(GraphEdge)...values(...)` touching those
    columns.

This is a static AST scan of the module's source, per docs/
TECHNICAL_ARCHITECTURE.md section 6.1: "No UPDATE ever touches object_id,
object_literal or valid_from. Supersession sets valid_to and superseded_by
on the old row and inserts a new one." Behavioural coverage of the same
rule lives in tests/integration/test_supersession.py, against a real
Postgres instance.

P1.2c adds the DB-level half: Postgres triggers (`nimbus_append_only`) make a
raw SQL UPDATE/DELETE of fact content fail even if the AST scan is bypassed
(see the trigger tests at the bottom of this file and
tests/integration/test_schema_pivot.py for the full matrix).
"""

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

GRAPH_MODULE = Path(__file__).resolve().parents[3] / "src" / "memory" / "graph.py"
FORBIDDEN_COLUMNS = {"object_id", "object_literal", "valid_from"}


def _resolve_update_target(call: ast.Call) -> str | None:
    """If `call` is `update(SomeModel)` (or `sa.update(SomeModel)`), return
    "SomeModel"; else None.
    """
    func = call.func
    is_update = (isinstance(func, ast.Name) and func.id == "update") or (
        isinstance(func, ast.Attribute) and func.attr == "update"
    )
    if not is_update or not call.args:
        return None
    arg = call.args[0]
    return arg.id if isinstance(arg, ast.Name) else None


def test_graph_repository_never_updates_immutable_columns():
    assert GRAPH_MODULE.exists(), (
        "src/memory/graph.py (GraphRepository) is not implemented yet — "
        "T2.9 must land it before this invariant is meaningful"
    )
    tree = ast.parse(GRAPH_MODULE.read_text(encoding="utf-8"), filename=str(GRAPH_MODULE))

    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        # Direct ORM attribute mutation, e.g. `edge.object_literal = x`.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in FORBIDDEN_COLUMNS:
                    violations.append((node.lineno, f"assignment to .{target.attr}"))
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Attribute) and node.target.attr in FORBIDDEN_COLUMNS:
                violations.append((node.lineno, f"augmented assignment to .{node.target.attr}"))
        # Core `update(GraphEdge).values(object_literal=...)`.
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "values"
        ):
            base = node.func.value
            if isinstance(base, ast.Call) and _resolve_update_target(base) == "GraphEdge":
                bad_kwargs = [kw.arg for kw in node.keywords if kw.arg in FORBIDDEN_COLUMNS]
                if bad_kwargs:
                    violations.append((node.lineno, f"update(GraphEdge).values({bad_kwargs})"))

    assert not violations, f"INV-3 violation(s) in src/memory/graph.py: {violations}"


# ---- DB-trigger half of INV-3 (P1.2c) -----------------------------------------------------


async def _seed_edge(session: AsyncSession) -> uuid.UUID:
    sid, nid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO session (id, user_id, mode, started_at, device_id) "
            "VALUES (:i, :u, 'b', :n, 'd')"
        ),
        {"i": sid, "u": uuid.uuid4(), "n": now},
    )
    await session.execute(
        text(
            "INSERT INTO graph_node (id, entity_type, canonical_name, created_at) "
            "VALUES (:i, 'person', 'x', :n)"
        ),
        {"i": nid, "n": now},
    )
    await session.execute(
        text(
            "INSERT INTO graph_edge (id, subject_id, predicate, object_literal, valid_from, "
            "ingested_at, source_session, confidence) "
            "VALUES (:i, :n, 'lives_in', 'Pune', :t, :t, :s, 0.9)"
        ),
        {"i": eid, "n": nid, "s": sid, "t": now},
    )
    return eid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE graph_edge SET object_literal = 'Delhi' WHERE id = :i",
        "UPDATE graph_edge SET valid_from = valid_from - interval '1 day' WHERE id = :i",
        "UPDATE graph_edge SET predicate = 'works_at' WHERE id = :i",
        "DELETE FROM graph_edge WHERE id = :i",
    ],
)
async def test_db_trigger_rejects_raw_sql_mutation_of_facts(db_session: AsyncSession, stmt: str):
    edge_id = await _seed_edge(db_session)
    with pytest.raises(DBAPIError, match="INV-3"):
        async with db_session.begin_nested():
            await db_session.execute(text(stmt), {"i": edge_id})


@pytest.mark.asyncio
async def test_db_trigger_allows_supersession_columns(db_session: AsyncSession):
    edge_id = await _seed_edge(db_session)
    result = await db_session.execute(
        text("UPDATE graph_edge SET valid_to = now() WHERE id = :i"), {"i": edge_id}
    )
    assert result.rowcount == 1  # type: ignore[attr-defined]
