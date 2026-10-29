# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Database canary sweep (P4.4; the INV-7 "canary sweep").

Scans every text-like column (`String`/`Text`/`Enum`, `JSON`/`JSONB`) of every
table in `Base.metadata` with the obfuscation-tolerant detector, and checks
the configured literal secrets (exact match). Encrypted columns (bytes) are
not readable here and are skipped; vectors and numbers are skipped.

The report holds table, column, category and a row count only: never a value,
a row id or any matched text (INV-7). Read-only: `SELECT` only.

`python -m src.app canary-sweep` runs it (exit 0 clean, 1 findings, 2 config
error). It needs `CANARY_SEED`: random per-process canaries would only find
leaks from the current process.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import JSON, MetaData, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from src.canary.detect import CanaryDetector

BATCH_ROWS = 500


@dataclass(frozen=True)
class SweepFinding:
    table: str
    column: str
    category: str  # "canary" | "secret" | "unscannable"
    rows: int


@dataclass(frozen=True)
class SweepReport:
    tables: int
    columns: int
    rows: int
    findings: tuple[SweepFinding, ...] = field(default=())

    @property
    def clean(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        head = f"canary sweep: tables={self.tables} columns={self.columns} rows={self.rows}"
        if self.clean:
            return head + " clean"
        parts = [f"{f.table}.{f.column}:{f.category}={f.rows}" for f in self.findings]
        return head + " FOUND " + " ".join(parts)


def _text_columns(table: object) -> list[str]:
    cols = []
    for col in getattr(table, "columns", ()):
        if isinstance(col.type, (String, JSON, JSONB)):
            cols.append(col.name)
    return cols


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None  # encrypted / binary: not readable here
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


async def sweep_database(
    session: AsyncSession,
    detector: CanaryDetector,
    *,
    metadata: MetaData,
    literals: Iterable[str] = (),
    batch_rows: int = BATCH_ROWS,
) -> SweepReport:
    lits = tuple(s for s in literals if isinstance(s, str) and len(s) >= 8)
    counts: Counter[tuple[str, str, str]] = Counter()
    n_tables = n_cols = n_rows = 0
    for table in metadata.sorted_tables:
        names = _text_columns(table)
        if not names:
            continue
        n_tables += 1
        n_cols += len(names)
        stmt = select(*(table.c[n] for n in names)).execution_options(yield_per=batch_rows)
        result = await session.stream(stmt)
        async for row in result:
            n_rows += 1
            for name, value in zip(names, row, strict=True):
                text = _as_text(value)
                if text is None:
                    continue
                hit = detector.detect(text)
                if hit is not None:
                    category = (
                        "unscannable" if hit.encoding in ("oversize", "too_complex") else "canary"
                    )
                    counts[(table.name, name, category)] += 1
                if lits and any(s in text for s in lits):
                    counts[(table.name, name, "secret")] += 1
    findings = tuple(SweepFinding(t, c, cat, n) for (t, c, cat), n in sorted(counts.items()))
    return SweepReport(n_tables, n_cols, n_rows, findings)
