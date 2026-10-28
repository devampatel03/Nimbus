# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Tool dispatch (ADR-0006): every model-proposed tool call is checked by a
deterministic `Policy` before anything runs (INV-5).

The policy (`src.policy`, P3.1) sees the proposed call (tool name, arguments,
the tool's code-declared risk) and a `PolicyContext` built here from
code-derived facts only: whether the tool was exposed this turn, the turn's
taint (P3.0 `Taint`, plus the cross-turn carry), the guard mode (kill switch /
spend cap), the enabled scopes and this tool's execution count in the last
hour. Model output supplies the tool name and arguments but never a verdict,
risk class, approval or taint flag. This module imports no LLM client code.

Fail closed: unknown tool, unexposed tool, policy error, and every verdict but
`allow` mean the tool does not run. `require_approval` is NOT executable until
P3.2 builds approvals: it is reported as a denial (`approval_unavailable`), and
this dispatcher never sets `user_confirmed` / `step_up_confirmed`.

`Risk`, `ProposedCall`, `Decision`, `Policy` and `DenyAllWritesPolicy` live in
`src.policy` and are re-exported here for existing importers.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from src.lanes.data import UntrustedText
from src.llm.types import ToolSpec
from src.policy import (
    LOCAL_SCOPES,
    Decision,
    DenyAllWritesPolicy,
    Policy,
    PolicyContext,
    PolicyMode,
    ProposedCall,
    Risk,
)

log = logging.getLogger(__name__)

__all__ = [
    "CALL_CAP_MESSAGE",
    "MAX_EXTERNAL_ITEMS",
    "MAX_TOOL_ERROR_CHARS",
    "MAX_TOOL_OUTPUT_CHARS",
    "TRUNCATION_MARKER",
    "Decision",
    "DenyAllWritesPolicy",
    "DispatchResult",
    "Dispatcher",
    "ExecutionCounter",
    "LoopTool",
    "Policy",
    "ProposedCall",
    "Risk",
    "ToolError",
    "ToolOutput",
    "safe_name",
]

# Tool output is attacker-influenced and re-enters the prompt, so it is bounded
# well below the LLM guard's pre-call spend estimate (a huge output must not be
# able to trip the daily cap and pause the model for the day).
MAX_TOOL_OUTPUT_CHARS = 4000
TRUNCATION_MARKER = "\n[truncated]"
MAX_TOOL_ERROR_CHARS = 200
CALL_CAP_MESSAGE = "Denied: too many tool calls in this turn."
# Each external item costs one quarantine call; bound them per tool output.
MAX_EXTERNAL_ITEMS = 5
APPROVAL_UNAVAILABLE = "approval_unavailable"
RATE_WINDOW_S = 3600.0


class ToolError(Exception):
    """Sanitised tool failure; the message is shown to the model."""


@dataclass(frozen=True)
class ToolOutput:
    text: str
    # Untrusted unless a tool explicitly opts out; rendered as a DataBlock.
    # `text` is for Nimbus-local text: it is data-framed, not quarantined.
    untrusted: bool = True
    # Externally authored free text (email bodies, invite descriptions, page
    # content). Each item is quarantined by the loop (ADR-0010 interim
    # default: every item, no bypass) or withheld. Never rendered raw.
    external: tuple[UntrustedText, ...] = ()


class LoopTool(Protocol):
    """A tool as the loop sees it. `risk` is declared by code, not the model."""

    spec: ToolSpec
    risk: Risk

    async def run(self, args: Mapping[str, Any]) -> ToolOutput: ...


@dataclass(frozen=True)
class DispatchResult:
    tool: str
    status: Literal["ok", "denied", "error"]
    content: str
    untrusted: bool = False
    rule_id: str = ""
    tainted: bool = False  # the taint the policy saw when this call was decided
    external: tuple[UntrustedText, ...] = ()  # at most MAX_EXTERNAL_ITEMS
    external_dropped: int = 0  # over the cap, or not UntrustedText (fail closed)
    needs_approval: bool = False  # policy said require_approval (not executed)
    step_up: bool = False  # ... and that approval would need a TOTP step-up


def safe_name(name: str) -> str:
    """Model-controlled; keep log lines single-line ASCII."""
    return str(name)[:64].encode("unicode_escape").decode("ascii")


ModeSource = Callable[[], Awaitable[object]]


class ExecutionCounter:
    """Executions per tool in the last hour (ADR-0009 row 4). In-process: a
    restart resets it (acceptable for a one-user bot; recorded in the P3.1 PR).
    Bounded memory: at most `limit` timestamps per tool, and only registered
    tool names are ever recorded (the dispatcher checks)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, limit: int = 1000) -> None:
        self._clock = clock
        self._seen: dict[str, deque[float]] = {}
        self._limit = limit

    def count(self, tool: str) -> int:
        q = self._seen.get(tool)
        if not q:
            return 0
        cutoff = self._clock() - RATE_WINDOW_S
        while q and q[0] <= cutoff:
            q.popleft()
        return len(q)

    def record(self, tool: str) -> None:
        self._seen.setdefault(tool, deque(maxlen=self._limit)).append(self._clock())


def _mode_of(raw: object) -> PolicyMode:
    """Map the guard's mode (an `LLMMode`, a StrEnum) onto the policy's mirror
    enum; anything unrecognised fails closed to KILLED."""
    try:
        return PolicyMode(str(raw)) if isinstance(raw, str) else PolicyMode.KILLED
    except ValueError:
        return PolicyMode.KILLED


class Dispatcher:
    def __init__(
        self,
        tools: Mapping[str, LoopTool],
        policy: Policy,
        *,
        mode_source: ModeSource | None = None,
        scopes_enabled: frozenset[str] = LOCAL_SCOPES,
        counter: ExecutionCounter | None = None,
    ) -> None:
        self._tools = dict(tools)
        self._policy = policy
        # None = no guard wired (unit tests). Every model call is gated by the
        # guard anyway, so a proposed call exists only if its model call was
        # allowed; production passes the guard's `mode` so a kill switch or
        # spend cap engaged mid-turn also stops the remaining tool calls.
        self._mode_source = mode_source
        self._scopes = frozenset(scopes_enabled)
        self._counter = counter or ExecutionCounter()

    async def _mode(self) -> PolicyMode:
        if self._mode_source is None:
            return PolicyMode.NORMAL
        try:
            return _mode_of(await self._mode_source())
        except Exception:  # noqa: BLE001 - unreadable mode fails closed
            log.warning("guard mode unreadable; treating as killed")
            return PolicyMode.KILLED

    async def _context(
        self, tool: LoopTool | None, name: str, exposed: bool, tainted: bool
    ) -> PolicyContext:
        risk = tool.risk if tool is not None else None
        return PolicyContext(
            exposed=exposed,
            tainted=tainted,
            mode=await self._mode(),
            scopes_enabled=self._scopes,
            calls_last_hour=self._counter.count(name) if tool is not None else 0,
            # No contact allowlist exists yet: every external write is treated
            # as going to an unlisted recipient (TOTP step-up). Fail closed.
            external_recipient=risk is Risk.WRITE_EXTERNAL,
            destructive=risk is Risk.DESTRUCTIVE,
            quiet_hours=False,  # reserved for proactive jobs (R11)
            # Approvals do not exist until P3.2: never confirmed here.
            user_confirmed=False,
            step_up_confirmed=False,
        )

    async def dispatch(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        exposed: frozenset[str],
        tainted: bool = False,
    ) -> DispatchResult:
        tainted = tainted is not False  # fail closed: only an explicit False is clean
        tool = self._tools.get(name) if isinstance(name, str) else None
        is_exposed = tool is not None and name in exposed
        call = ProposedCall(tool=name, args=args, risk=tool.risk if tool is not None else None)
        decision: Decision | None
        try:
            decision = self._policy.decide(
                call, await self._context(tool, name, is_exposed, tainted)
            )
            if not isinstance(decision, Decision):
                raise TypeError("policy returned a non-Decision")
        except Exception:  # noqa: BLE001 - fail closed
            decision = None
        verdict = decision.verdict if decision is not None else "deny"
        rule_id = decision.rule_id if decision is not None else "deny.error"
        step_up = decision is not None and decision.step_up is True
        runnable = verdict == "allow" and is_exposed and tool is not None
        # One structured line per decision: names and ids only, never arguments.
        log.info(
            "dispatch decision tool=%s verdict=%s rule_id=%s step_up=%s tainted=%s executed=%s",
            safe_name(name),
            verdict,
            safe_name(rule_id),
            "yes" if step_up else "no",
            "yes" if tainted else "no",
            "yes" if runnable else "no",
        )
        if decision is None:
            res = DispatchResult(name, "denied", "Denied: policy error.", rule_id="deny.error")
        elif not runnable or tool is None:
            res = _denied(name, decision, step_up)
        else:
            self._counter.record(name)
            res = await self._run(name, tool, args, rule_id)
        return replace(res, tainted=tainted)

    async def _run(
        self, name: str, tool: LoopTool, args: Mapping[str, Any], rule_id: str
    ) -> DispatchResult:
        try:
            out = await tool.run(args)
            text = "" if out.text is None else str(out.text)
            untrusted = bool(out.untrusted)
            raw_external = out.external if isinstance(out.external, tuple) else ()
            valid = tuple(u for u in raw_external if isinstance(u, UntrustedText))
            external = valid[:MAX_EXTERNAL_ITEMS]
            dropped = len(raw_external) - len(external)
            if not isinstance(out.external, tuple) and out.external:
                dropped += 1
        except ToolError as e:
            # Error text may echo external content: capped and treated as DATA.
            msg = str(e)[:MAX_TOOL_ERROR_CHARS]
            return DispatchResult(
                name, "error", f"Tool error: {msg}", untrusted=True, rule_id=rule_id
            )
        except Exception:  # noqa: BLE001 - fail closed
            log.warning("tool failure tool=%s", safe_name(name))
            return DispatchResult(name, "error", "Tool error: failed.", rule_id=rule_id)
        if len(text) > MAX_TOOL_OUTPUT_CHARS:
            text = text[:MAX_TOOL_OUTPUT_CHARS] + TRUNCATION_MARKER
        return DispatchResult(
            name,
            "ok",
            text,
            untrusted=untrusted,
            rule_id=rule_id,
            external=external,
            external_dropped=dropped,
        )


def _denied(name: str, decision: Decision, step_up: bool) -> DispatchResult:
    rule_id = decision.rule_id
    if decision.verdict == "require_approval":
        # P3.2 turns this into an approval card; until then it never runs.
        return DispatchResult(
            name,
            "denied",
            f"Denied by policy ({rule_id}): approval required, but approvals are not "
            f"available yet ({APPROVAL_UNAVAILABLE}).",
            rule_id=rule_id,
            needs_approval=True,
            step_up=step_up,
        )
    return DispatchResult(
        name, "denied", f"Denied by policy ({rule_id}): {decision.reason}.", rule_id=rule_id
    )
