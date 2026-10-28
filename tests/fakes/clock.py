# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Injectable fake clock for scheduler tests (R11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FakeClock:
    """Aware-UTC clock that only moves when the test says so."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 27, 4, 30, tzinfo=UTC)  # 10:00 IST
        if self._now.tzinfo is None:
            raise ValueError("FakeClock needs an aware datetime")

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when
