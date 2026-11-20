# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Supervision: crash -> logged by type name -> restarted with capped backoff."""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.app.supervisor import EXIT_FATAL_LOOP, EXIT_OK, LoopSpec, Supervisor

SECRET = "123456789:AAH_fake_token_for_tests_only_0123456789"  # pragma: allowlist secret


class Fatal(Exception):
    pass


async def test_crashing_loop_restarts_with_doubling_capped_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    delays: list[float] = []
    calls = 0

    async def sleep(s: float) -> None:
        delays.append(s)

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls >= 7:
            stop.set()
            await asyncio.sleep(10)
        raise RuntimeError(f"boom with {SECRET} and SELECT secret_column")

    sup = Supervisor(
        [LoopSpec("gateway", flaky)], stop=stop, backoff_initial_s=1, backoff_max_s=8, sleep=sleep
    )
    with caplog.at_level(logging.ERROR):
        assert await asyncio.wait_for(sup.serve(), 10) == EXIT_OK
    assert delays == [1, 2, 4, 8, 8, 8]
    assert sup.crashes == {"gateway": 6}
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "err=RuntimeError" in text
    assert SECRET not in text and "secret_column" not in text  # type name only
    assert all(r.exc_info is None for r in caplog.records)


async def test_a_loop_that_returns_is_treated_as_a_crash() -> None:
    stop = asyncio.Event()
    calls = 0

    async def quits() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            stop.set()
            await asyncio.sleep(10)

    async def sleep(_: float) -> None:
        return None

    sup = Supervisor([LoopSpec("w", quits)], stop=stop, sleep=sleep)
    await asyncio.wait_for(sup.serve(), 10)
    assert calls == 3 and sup.crashes["w"] == 2


async def test_one_crashing_loop_does_not_stop_the_others() -> None:
    stop = asyncio.Event()
    ticks = 0

    async def healthy() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            if ticks == 20:
                stop.set()
            await asyncio.sleep(0)

    async def broken() -> None:
        raise ValueError

    async def sleep(_: float) -> None:
        await asyncio.sleep(0)

    sup = Supervisor(
        [LoopSpec("a", healthy), LoopSpec("b", broken)], stop=stop, sleep=sleep, grace_s=2
    )
    assert await asyncio.wait_for(sup.serve(), 10) == EXIT_OK
    assert ticks >= 20 and sup.crashes["b"] >= 1 and "a" not in sup.crashes


async def test_fatal_error_is_not_retried_and_stops_the_process() -> None:
    stop = asyncio.Event()
    calls = 0

    async def revoked() -> None:
        nonlocal calls
        calls += 1
        raise Fatal

    async def forever() -> None:
        await asyncio.sleep(60)

    sup = Supervisor([LoopSpec("g", revoked, fatal=(Fatal,)), LoopSpec("w", forever)], stop=stop)
    assert await asyncio.wait_for(sup.serve(), 10) == EXIT_FATAL_LOOP
    assert calls == 1 and sup.fatal == "Fatal" and stop.is_set()


async def test_stop_cancels_loops_gracefully() -> None:
    stop = asyncio.Event()
    cleaned: list[str] = []

    async def loop() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.append("done")

    sup = Supervisor([LoopSpec("a", loop)], stop=stop)
    task = asyncio.create_task(sup.serve())
    await asyncio.sleep(0.05)
    stop.set()
    assert await asyncio.wait_for(task, 5) == EXIT_OK
    assert cleaned == ["done"]


async def test_backoff_resets_after_a_healthy_run() -> None:
    stop = asyncio.Event()
    t = [0.0]
    delays: list[float] = []
    n = 0

    async def sleep(s: float) -> None:
        delays.append(s)

    async def loop() -> None:
        nonlocal n
        n += 1
        if n == 4:
            stop.set()
            await asyncio.sleep(10)
        if n == 3:
            t[0] += 1000  # ran healthily for a long time before crashing
        raise RuntimeError

    sup = Supervisor(
        [LoopSpec("a", loop)],
        stop=stop,
        backoff_initial_s=1,
        reset_after_s=300,
        sleep=sleep,
        clock=lambda: t[0],
    )
    await asyncio.wait_for(sup.serve(), 10)
    assert delays == [1, 2, 1]
