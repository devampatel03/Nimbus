# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Outbound rate limiter with a fake clock (flood tests)."""

from __future__ import annotations

from src.gateway.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


async def test_per_chat_one_per_second() -> None:
    c = Clock()
    rl = RateLimiter(per_chat_interval_s=1.0, global_interval_s=0.0, clock=c.now, sleep=c.sleep)
    for _ in range(100):
        await rl.acquire(1)
    assert 98.9 <= c.t - 1000.0 <= 99.1  # first is free, 99 more at 1s


async def test_global_ceiling_across_chats() -> None:
    c = Clock()
    rl = RateLimiter(per_chat_interval_s=1.0, global_interval_s=1 / 30, clock=c.now, sleep=c.sleep)
    for chat in range(300):
        await rl.acquire(chat)
    # 300 distinct chats, 30/s -> at least ~9.96 s
    assert c.t - 1000.0 >= 9.9


async def test_penalize_defers_next_send() -> None:
    c = Clock()
    rl = RateLimiter(clock=c.now, sleep=c.sleep)
    await rl.acquire(1)
    rl.penalize(1, 30)
    await rl.acquire(1)
    assert c.t - 1000.0 >= 30
