# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P2.4: static guards over `src/memory/store.py` (no DB needed).

* INV-3: the store never DELETEs and only UPDATEs the columns the
  `nimbus_append_only` trigger allows to change on `memory_chunk`.
* R8: the search SQL keeps the promoted filter, and it is a bound parameter.
* Parameterised SQL: no f-string/format/concatenation builds a statement.
"""

import ast
import re
from pathlib import Path

from src.memory import store
from src.memory.schemas.triggers import APPEND_ONLY_TABLES

SRC = Path(store.__file__).read_text(encoding="utf-8")


def test_store_never_deletes_and_only_updates_trigger_mutable_columns() -> None:
    assert not re.search(r"\bDELETE\b", SRC, re.IGNORECASE)
    assert "TRUNCATE" not in SRC.upper()
    mutable = set(APPEND_ONLY_TABLES["memory_chunk"][0])
    updates = re.findall(r"UPDATE\s+(\w+)\s+SET\s+(.*?)\s+WHERE", SRC, re.DOTALL | re.IGNORECASE)
    assert updates, "expected the invalidate/promote UPDATEs"
    for table, assignments in updates:
        assert table == "memory_chunk"
        cols = set(re.findall(r"(\w+)\s*=", assignments))
        assert cols <= mutable, f"UPDATE touches immutable columns: {cols - mutable}"
        assert not cols & {"content", "embedding", "lane", "source_event_id", "created_at"}


def test_search_sql_filters_unpromoted_with_a_bound_parameter() -> None:
    assert "(c.promoted OR :unp)" in store._SEARCH_SQL
    assert "c.invalidated_at IS NULL" in store._SEARCH_SQL


def test_no_dynamic_sql_construction() -> None:
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):  # f-string
            text_parts = "".join(
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            assert not re.search(r"\b(SELECT|UPDATE|INSERT|DELETE)\b", text_parts, re.IGNORECASE)
