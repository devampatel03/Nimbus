# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Tool dispatch (ADR-0006): every model-proposed tool call is checked by a
deterministic `Policy` before anything runs (INV-5).

The policy sees only code-derived facts (tool name, the tool's declared risk
class, whether the tool was exposed this turn). Model output supplies the
tool name and arguments but never a verdict, risk class or approval flag.
This module imports no LLM code. Fail closed: unknown tool, unexposed tool,
policy error, or any non-`allow` verdict means the tool does not run.

P3.1 swaps `DenyAllWritesPolicy` for the real policy engine.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from src.llm.types import ToolSpec

log = logging.getLogger(__name__)

# Tool output is attacker-influenced and re-enters the prompt, so it is bounded
# well below the LLM guard's pre-call spend estimate (a huge output must not be
# able to trip the daily cap and pause the model for the day).
MAX_TOOL_OUTPUT_CHARS = 4000
TRUNCATION_MARKER = "\n[truncated]"
CALL_CAP_MESSAGE = "Denied: too many tool calls in this turn."


class Risk(StrEnum):
    READ = "read"
    WRITE_INTERNAL = "write_internal"
    WRITE_EXTERNAL = "write_external"
    DESTRUCTIVE = "destructive"


class ToolError(Exception):
    """Sanitised tool failure; the message is shown to the model."""


@dataclass(frozen=True)
class ToolOutput:
    text: str
    # Untrusted unless a tool explicitly opts out; rendered as a DataBlock.
    untrusted: bool = True


class LoopTool(Protocol):
    """A tool as the loop sees it. `risk` is declared by code, not the model."""

    spec: ToolSpec
    risk: Risk

    async def run(self, args: Mapping[str, Any]) -> ToolOutput: ...


@dataclass(frozen=True)
class ProposedCall:
    tool: str
    args: Mapping[str, Any]
    risk: Risk | None  # None = unknown tool


@dataclass(frozen=True)
class Decision:
    verdict: Literal["allow", "require_approval", "deny"]
    rule_id: str
    reason: str


class Policy(Protocol):
    """Pure and deterministic. No I/O, no model."""

    def decide(self, call: ProposedCall) -> Decision: ...


class DenyAllWritesPolicy:
    """Interim policy: READ is allowed; everything else (any write or
    external effect, and any unknown tool) is denied."""

    def decide(self, call: ProposedCall) -> Decision:
        if call.risk is None:
            return Decision(verdict="deny", rule_id="deny.unknown_tool", reason="unknown tool")
        if call.risk is Risk.READ:
            return Decision(verdict="allow", rule_id="allow.read", reason="read-only")
        return Decision(
            verdict="deny", rule_id="deny.write_interim", reason="writes are disabled for now"
        )


@dataclass(frozen=True)
class DispatchResult:
    tool: str
    status: Literal["ok", "denied", "error"]
    content: str
    untrusted: bool = False
    rule_id: str = ""


class Dispatcher:
    def __init__(self, tools: Mapping[str, LoopTool], policy: Policy) -> None:
        self._tools = dict(tools)
        self._policy = policy

    async def dispatch(
        self, name: str, args: Mapping[str, Any], *, exposed: frozenset[str]
    ) -> DispatchResult:
        result = await self._decide_and_run(name, args, exposed)
        log.info(
            "dispatch decision tool=%s verdict=%s rule_id=%s",
            name[:64].encode("ascii", "backslashreplace").decode(),
            "deny" if result.status == "denied" else "allow",
            result.rule_id,
        )
        return result

    async def _decide_and_run(
        self, name: str, args: Mapping[str, Any], exposed: frozenset[str]
    ) -> DispatchResult:
        tool = self._tools.get(name) if name in exposed else None
        call = ProposedCall(tool=name, args=args, risk=tool.risk if tool else None)
        try:
            decision = self._policy.decide(call)
        except Exception:  # noqa: BLE001 - fail closed
            log.warning("policy error tool=%s", name)
            return DispatchResult(name, "denied", "Denied: policy error.", rule_id="deny.error")
        if decision.verdict != "allow" or tool is None:
            # require_approval is not executable until P3.2; treated as deny.
            return DispatchResult(
                name,
                "denied",
                f"Denied by policy ({decision.rule_id}): {decision.reason}.",
                rule_id=decision.rule_id,
            )
        try:
            out = await tool.run(args)
        except ToolError as e:
            return DispatchResult(name, "error", f"Tool error: {e}", rule_id=decision.rule_id)
        except Exception:  # noqa: BLE001 - fail closed
            log.warning("tool failure tool=%s", name)
            return DispatchResult(name, "error", "Tool error: failed.", rule_id=decision.rule_id)
        text = out.text
        if len(text) > MAX_TOOL_OUTPUT_CHARS:
            text = text[:MAX_TOOL_OUTPUT_CHARS] + TRUNCATION_MARKER
        return DispatchResult(name, "ok", text, untrusted=out.untrusted, rule_id=decision.rule_id)
