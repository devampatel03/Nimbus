# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Minimal pgvector `vector(n)` column type.

The `pgvector` Python package is not a dependency yet; this type binds/reads
vectors via pgvector's text form (`[1,2,3]`), which needs no client codec.
Swap for `pgvector.sqlalchemy.Vector` when the dep lands (same DDL).
"""

from typing import Any

from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dim})"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            if len(value) != self.dim:
                raise ValueError(f"expected {self.dim} dims, got {len(value)}")
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: str | None) -> list[float] | None:
            if value is None:
                return None
            return [float(x) for x in value.strip("[]").split(",") if x]

        return process
