# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Request limiter for free-tier and rate-limited keys (P5.5, ADR-0025).

One `RequestLimiter` per `GuardedLLM`, consulted in the guard after the scrub
and before the spend-cap reservation. It is a deterministic local gate, not a
model decision:

- at most `rpm` provider requests in any rolling 60 s window;
- at most `daily` requests per IST day;
- background purposes (memory extraction, friction, eval) may use only
  `background_share` of both, and never wait: they are refused at once
  (`LLMRefused("rate_limited")`) and the job defers. Chat always has headroom;
- after a provider 429 the limiter cools down for the Retry-After (bounded).
  Background calls are refused for the whole cool-down; a chat call waits for
  it only if it ends within `max_wait_s`, else it is refused and the user gets
  the fixed "busy" reply.

Counts are process-local (one process, ADR-0012). A restart forgets the
day's count; the provider's own quota is the backstop (documented in
ADR-0025). The limiter counts guard calls; the adapter's own bounded retries
after a 429/5xx are extra provider requests inside the same guard call.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.llm.types import LLMRefused, Purpose

logger = logging.getLogger("nimbus.llm")

IST = ZoneInfo("Asia/Kolkata")
WINDOW = timedelta(seconds=60)
DEFAULT_COOLDOWN_S = 30.0
MAX_COOLDOWN_S = 120.0
_MAX_WAITS = 8  # bound on sleep/recheck rounds for one acquire

# A user is waiting on these; everything else is background and yields.
FOREGROUND_PURPOSES = frozenset({Purpose.CHAT, Purpose.QUARANTINE_EXTRACT, Purpose.TRANSCRIBE})

Sleep = Callable[[float], Awaitable[None]]


def is_foreground(purpose: Purpose) -> bool:
    return purpose in FOREGROUND_PURPOSES


class RequestLimiter:
    def __init__(
        self,
        *,
        rpm: int,
        daily: int,
        background_share: float,
        max_wait_s: float,
        clock: Callable[[], datetime],
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rpm < 1 or daily < 1 or not 0 < background_share <= 1 or max_wait_s < 0:
            raise ValueError("invalid rate limits")
        self._rpm = rpm
        self._daily = daily
        self._bg_rpm = max(1, int(rpm * background_share))
        self._bg_daily = max(1, int(daily * background_share))
        self._max_wait = max_wait_s
        self._clock = clock
        self._sleep = sleep
        self._recent: deque[datetime] = deque()
        self._day = ""
        self._day_count = 0
        self._cooldown_until: datetime | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- state

    def _roll(self, now: datetime) -> None:
        while self._recent and now - self._recent[0] >= WINDOW:
            self._recent.popleft()
        day = now.astimezone(IST).date().isoformat()
        if day != self._day:
            self._day, self._day_count = day, 0

    @property
    def requests_today(self) -> int:
        return self._day_count

    def note_rate_limited(self, retry_after_s: float | None) -> None:
        """A provider 429: pause background calls (and make chat wait) for the
        provider's Retry-After, bounded to `MAX_COOLDOWN_S`."""
        hint = DEFAULT_COOLDOWN_S if retry_after_s is None else retry_after_s
        delay = min(max(hint, 1.0), MAX_COOLDOWN_S)
        until = self._clock() + timedelta(seconds=delay)
        if self._cooldown_until is None or until > self._cooldown_until:
            self._cooldown_until = until
        logger.warning("llm provider rate limited; cooling down for %.0fs", delay)

    # ----------------------------------------------------------- acquire

    def _wait_needed(self, now: datetime, foreground: bool) -> float | None:
        """0.0 = go now; >0 = seconds until a slot may free up; None = refuse
        (daily allowance used up, which waiting cannot fix)."""
        self._roll(now)
        daily = self._daily if foreground else self._bg_daily
        if self._day_count >= daily:
            return None
        if self._cooldown_until is not None and now < self._cooldown_until:
            return (self._cooldown_until - now).total_seconds()
        rpm = self._rpm if foreground else self._bg_rpm
        if len(self._recent) >= rpm:
            oldest = self._recent[len(self._recent) - rpm]
            return max((oldest + WINDOW - now).total_seconds(), 0.001)
        return 0.0

    async def acquire(self, purpose: Purpose) -> None:
        """Take one request slot or raise `LLMRefused("rate_limited")`."""
        foreground = is_foreground(purpose)
        waited = 0.0
        for _ in range(_MAX_WAITS + 1):
            async with self._lock:
                now = self._clock()
                wait = self._wait_needed(now, foreground)
                if wait == 0.0:
                    self._recent.append(now)
                    self._day_count += 1
                    return
            if wait is None or not foreground or waited + wait > self._max_wait:
                reason = "daily" if wait is None else ("background" if not foreground else "wait")
                logger.info("llm request refused: rate_limited (%s)", reason)
                raise LLMRefused("rate_limited")
            await self._sleep(wait)
            waited += wait
        raise LLMRefused("rate_limited")


__all__ = ["FOREGROUND_PURPOSES", "RequestLimiter", "is_foreground"]
