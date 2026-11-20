# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Which stored event the current turn answers (P3.3 M5).

`TurnWorker` sets it around `run_turn`; the scheduling tools read it to stamp
`source_event_id` on what they create. That id is code-derived (the gateway's
stored event), never model output, and it is what makes a replayed turn
idempotent: the database refuses a second reminder or task with the same
`(source_event_id, title[, remind_at])`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_event_id: ContextVar[uuid.UUID | None] = ContextVar("nimbus_turn_event_id", default=None)


def current_event_id() -> uuid.UUID | None:
    return _current_event_id.get()


@contextmanager
def turn_event(event_id: uuid.UUID | None) -> Iterator[None]:
    token = _current_event_id.set(event_id)
    try:
        yield
    finally:
        _current_event_id.reset(token)
