# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

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
"""

import ast
from pathlib import Path

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
