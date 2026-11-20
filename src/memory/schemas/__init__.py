# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""SQLAlchemy models for the memory store. Import this package (not an
individual module) wherever `Base.metadata` must be complete — e.g. Alembic
`env.py` — so every table is registered before autogenerate/create_all runs.

Importing this package also wires the extension/function/trigger DDL onto
`Base.metadata` so `create_all()` (pytest) yields the same append-only
guarantees as the Alembic migration.
"""

from src.memory.schemas.base import Base
from src.memory.schemas.commitment import Commitment
from src.memory.schemas.decision import Decision
from src.memory.schemas.event import Event
from src.memory.schemas.graph import GraphEdge, GraphNode
from src.memory.schemas.ledger import CommitmentLedger
from src.memory.schemas.memory_chunk import MemoryChunk
from src.memory.schemas.ops import (
    InboundUpdate,
    Job,
    LLMAudit,
    PendingAction,
    PolicyAudit,
    SystemFlag,
)
from src.memory.schemas.person import Person
from src.memory.schemas.scheduling import Reminder, Task
from src.memory.schemas.session import Session
from src.memory.schemas.triggers import attach_table_triggers, install_orm_ddl

install_orm_ddl(Base.metadata)
for _table in Base.metadata.tables.values():
    attach_table_triggers(_table)

__all__ = [
    "Base",
    "Commitment",
    "CommitmentLedger",
    "Decision",
    "Event",
    "GraphEdge",
    "GraphNode",
    "InboundUpdate",
    "Job",
    "LLMAudit",
    "MemoryChunk",
    "PendingAction",
    "Person",
    "PolicyAudit",
    "Reminder",
    "Session",
    "SystemFlag",
    "Task",
]
