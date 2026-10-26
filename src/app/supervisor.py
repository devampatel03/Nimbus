# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Loop supervision: run named loops side by side, restart a crashed one with
capped exponential backoff, stop everything on request.

A crash is logged by exception TYPE NAME only (INV-7): exception messages and
tracebacks can embed SQL text, URLs or user content. A loop that ends without
being asked to is treated as a crash. A `fatal` exception type (a revoked bot
token, say) is not retried: it stops the whole process with a distinct exit code
so the operator sees it instead of an endless restart cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FATAL_LOOP = 4


class LoopExited(Exception):
    """A loop returned although nobody asked it to stop."""


@dataclass(frozen=True)
class LoopSpec:
    name: str
    main: Callable[[], Awaitable[None]]
    fatal: tuple[type[BaseException], ...] = ()


class Supervisor:
    def __init__(
        self,
        specs: Sequence[LoopSpec],
        *,
        stop: asyncio.Event,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 60.0,
        reset_after_s: float = 300.0,
        grace_s: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._specs = list(specs)
        self._stop = stop
        self._initial = backoff_initial_s
        self._max = backoff_max_s
        self._reset_after = reset_after_s
        self._grace = grace_s
        self._sleep = sleep
        self._clock = clock
        self.crashes: dict[str, int] = {}
        self.fatal: str | None = None  # exception type name

    async def serve(self) -> int:
        """Returns when `stop` is set (exit 0) or a fatal loop error occurred (exit 4)."""
        tasks = [
            asyncio.create_task(self._supervise(s), name=f"loop:{s.name}") for s in self._specs
        ]
        stopper: asyncio.Task[object] = asyncio.create_task(self._stop.wait(), name="loop:stop")
        try:
            await asyncio.wait([stopper, *tasks], return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._stop.set()
            stopper.cancel()
            for t in tasks:
                t.cancel()
            everything: list[asyncio.Task[Any]] = [stopper, *tasks]
            done, pending = await asyncio.wait(everything, timeout=self._grace)
            for t in pending:
                log.error("loop did not stop within grace task=%s", t.get_name())
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    log.error("loop supervisor error err=%s", type(t.exception()).__name__)
        return EXIT_FATAL_LOOP if self.fatal is not None else EXIT_OK

    async def _pause(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _supervise(self, spec: LoopSpec) -> None:
        delay = self._initial
        while not self._stop.is_set():
            started = self._clock()
            try:
                await spec.main()
                raise LoopExited(spec.name)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if isinstance(exc, KeyboardInterrupt | SystemExit):
                    raise
                if self._stop.is_set():
                    return
                if spec.fatal and isinstance(exc, spec.fatal):
                    log.critical(
                        "loop failed fatally loop=%s err=%s", spec.name, type(exc).__name__
                    )
                    self.fatal = type(exc).__name__
                    self._stop.set()
                    return
                self.crashes[spec.name] = self.crashes.get(spec.name, 0) + 1
                if self._clock() - started >= self._reset_after:
                    delay = self._initial  # it ran healthily for a while
                log.error(
                    "loop crashed loop=%s err=%s restart_in=%.0fs",
                    spec.name,
                    type(exc).__name__,
                    delay,
                )
                await self._pause(delay)
                delay = min(delay * 2, self._max)
