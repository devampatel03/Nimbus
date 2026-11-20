# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Injectable clock (TARGET_DESIGN section 1). Time is stored in UTC; code that
needs "now" takes a `Clock` so tests can use a fake one (R11)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware (UTC)."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
