# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Outbound pacing: ~1 msg/s per chat plus a global ceiling (Telegram allows
about 30 msg/s overall). Slots are reserved synchronously (no await between
read and write of the schedule), so concurrent senders queue in order."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    def __init__(
        self,
        *,
        per_chat_interval_s: float = 1.0,
        global_interval_s: float = 1 / 30,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._chat_iv = per_chat_interval_s
        self._glob_iv = global_interval_s
        self._clock = clock
        self._sleep = sleep
        self._chat_next: dict[int, float] = {}
        self._global_next = 0.0

    async def acquire(self, chat_id: int) -> None:
        now = self._clock()
        slot = max(now, self._chat_next.get(chat_id, 0.0), self._global_next)
        self._chat_next[chat_id] = slot + self._chat_iv
        self._global_next = slot + self._glob_iv
        if slot > now:
            await self._sleep(slot - now)

    def penalize(self, chat_id: int, seconds: float) -> None:
        """After a 429, push the next slot out by `retry_after`."""
        until = self._clock() + seconds
        self._chat_next[chat_id] = max(self._chat_next.get(chat_id, 0.0), until)
        self._global_next = max(self._global_next, until)
