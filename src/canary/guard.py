# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Outbound canary guard (P4.4, ADR-0023, INV-7, INV-6).

The model is assumed compromised by anything it reads, so it may try to leak
what it was shown. Canaries are planted where it reads (system prompt, memory
preamble, tool-output frames; `src.canary.tokens`), and every outbound path
asks this guard first:

* the final model reply (`Orchestrator`) and every Telegram send
  (`CanaryGateway`): replaced by `BLOCKED_REPLY`;
* tool arguments (`Dispatcher`): the call is denied (`deny.canary`) and never
  runs, approval cards included;
* third-party HTTP requests (`EgressClient` outbound check, every service but
  the model provider): refused before any I/O;
* logs: the scrubber redacts canary values and any obfuscated form.

A hit is recorded as a `policy_audit` row (`event=canary_blocked`,
`rule_id=deny.canary`, `reason=<sink>:<site>:<encoding>`: fixed codes only,
never the value or the text) and trips the alert hook: a fixed-text Telegram
notice to the allowlisted user, at most once per cooldown, sent in the
background so a blocked send can never wait on (or deadlock with) its alert.

Fail closed: a detector error counts as a hit; an audit or alert failure is
logged by type and does not unblock anything.

Stream (`stream=True`: Telegram sends, tool-argument values): the last
`STREAM_WINDOW` skeleton characters of earlier outbound items are kept (in
memory, never logged) and checked together with the new item: a canary cut
over consecutive items is caught once 10 contiguous characters are out, and
pieces sent out of order once two different 6-character pieces are out. The
window is not cleared on a hit, so the pieces after a blocked one stay blocked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

from src.approvals.types import AuditRecord
from src.canary.detect import FRAGMENT_CHARS, CanaryDetector, CanaryHit, skeleton
from src.canary.tokens import CanarySet, Site

log = logging.getLogger(__name__)

BLOCKED_REPLY = (
    "I didn't send my reply: it contained protected internal data, which can mean "
    "something I read was trying to make me leak it. Nothing else was sent."
)
DENIED_TOOL = "Denied: the arguments contain protected internal data (deny.canary)."
ALERT_TEXT = (
    "Security alert: Nimbus blocked an outgoing message or action because it contained "
    "a canary marker (possible prompt injection or data exfiltration). Nothing was sent. "
    "The policy audit log has the details (event canary_blocked)."
)
RULE_ID = "deny.canary"
STREAM_WINDOW = 2048
EVENT = "canary_blocked"


class CanaryBlocked(Exception):
    """An outbound item was refused. Fixed codes only, never the text."""

    def __init__(self, sink: str) -> None:
        self.sink = sink
        super().__init__(f"canary blocked sink={sink}")


class AuditSink(Protocol):
    async def audit(self, rec: AuditRecord) -> None: ...


AlertSender = Callable[[str], Awaitable[object]]


def _code(value: str, limit: int) -> str:
    return "".join(c if c.isalnum() or c in "_.:+>-" else "_" for c in value)[:limit]


class CanaryGuard:
    def __init__(
        self,
        canaries: CanarySet,
        *,
        configured: Iterable[str] = (),
        audit: AuditSink | None = None,
        alert: AlertSender | None = None,
        alert_cooldown_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._canaries = canaries
        entries: list[tuple[str, str, bool]] = [(c.site.value, c.value, True) for c in canaries]
        entries += [("configured", v, False) for v in configured if isinstance(v, str)]
        self._detector = CanaryDetector(entries)
        self._audit = audit
        self._alert = alert
        self._cooldown = alert_cooldown_s
        self._clock = clock
        self._last_alert: float | None = None
        self._window = ""  # stream window (skeleton chars only)
        self._tasks: set[asyncio.Task[None]] = set()
        self.hits = 0  # process-local counter (health / tests)

    def __repr__(self) -> str:
        return f"CanaryGuard(canaries={self._canaries!r}, hits={self.hits})"

    # -------------------------------------------------------------- wiring

    def bind_audit(self, audit: AuditSink) -> None:
        self._audit = audit

    def bind_alert(self, alert: AlertSender | None) -> None:
        self._alert = alert

    @property
    def canaries(self) -> CanarySet:
        return self._canaries

    @property
    def detector(self) -> CanaryDetector:
        return self._detector

    def plant(self, site: Site) -> str:
        return self._canaries.plant(site)

    # ----------------------------------------------------------- detection

    def detect(self, text: str) -> CanaryHit | None:
        """Stateless check. Never raises: a detector error is a hit."""
        try:
            return self._detector.detect(text)
        except Exception:  # noqa: BLE001 - fail closed
            return CanaryHit("unknown", "detector_error")

    def log_matcher(self, text: str) -> bool:
        """For the log scrubber: True = redact the whole text."""
        return self.detect(text) is not None

    def _stream_hit(self, text: str) -> CanaryHit | None:
        """A canary spread over several outbound items (see module docstring)."""
        try:
            sk = skeleton(text)
            window = self._window
            joined = window + sk
            seam = joined[max(0, len(window) - (FRAGMENT_CHARS - 1)) :]
            got = self._detector.match_skeleton(seam)
            encoding = "split"
            if got is None:
                got = self._detector.scattered(joined, new_from=len(window))
                encoding = "scattered"
            self._window = joined[-STREAM_WINDOW:]
        except Exception:  # noqa: BLE001 - fail closed
            return CanaryHit("unknown", "detector_error")
        return CanaryHit(got[0], encoding) if got is not None else None

    async def screen(
        self, text: str, *, sink: str, tool: str | None = None, stream: bool = False
    ) -> bool:
        """True when `text` may go out. On a hit: audited, alerted, False."""
        hit = self.detect(text)
        if hit is None and stream:
            hit = self._stream_hit(text if isinstance(text, str) else str(text))
        if hit is None:
            return True
        await self.report(hit, sink=sink, tool=tool)
        return False

    async def require_clean(self, text: str, *, sink: str, tool: str | None = None) -> None:
        if not await self.screen(text, sink=sink, tool=tool):
            raise CanaryBlocked(_code(sink, 32))

    # ------------------------------------------------------------ reporting

    async def report(self, hit: CanaryHit, *, sink: str, tool: str | None = None) -> None:
        self.hits += 1
        sink_code = _code(sink, 24)
        reason = _code(f"{sink_code}:{hit.site}:{hit.encoding}", 64)
        log.error(
            "canary blocked sink=%s site=%s encoding=%s",
            sink_code,
            _code(hit.site, 16),
            _code(hit.encoding, 32),
        )
        if self._audit is not None:
            try:
                await self._audit.audit(
                    AuditRecord(
                        kind="decision",
                        event=EVENT,
                        tool=_code(tool, 64) if tool else None,
                        rule_id=RULE_ID,
                        outcome="deny",
                        reason=reason,
                        executed=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - still blocked
                log.warning("canary audit write failed err=%s", type(exc).__name__)
        self._maybe_alert()

    def _maybe_alert(self) -> None:
        if self._alert is None:
            return
        now = self._clock()
        if self._last_alert is not None and now - self._last_alert < self._cooldown:
            return
        self._last_alert = now
        try:
            task = asyncio.get_running_loop().create_task(self._send_alert(self._alert))
        except RuntimeError:
            log.warning("canary alert skipped: no running loop")
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _send_alert(alert: AlertSender) -> None:
        try:
            await alert(ALERT_TEXT)
        except Exception as exc:  # noqa: BLE001 - the block already happened
            log.warning("canary alert failed err=%s", type(exc).__name__)

    async def drain(self) -> None:
        """Wait for pending alert sends (shutdown and tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
