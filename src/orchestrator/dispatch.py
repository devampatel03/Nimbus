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
`allow` mean the tool does not run.

Approvals (P3.2, ADR-0009): `dispatch()` (the model's path) NEVER sets
`user_confirmed` / `step_up_confirmed`. On `require_approval` it hands the call
to the injected `ApprovalRequester`, which stores it and sends the user a card;
the call does not run. Without a requester it is reported as a denial
(`approval_unavailable`). The only way to a confirmed context is
`execute_approved(approval)`, whose argument is a sealed `ConsumedApproval`
minted only by `ApprovalStore.consume` after the atomic `pending -> consumed`
UPDATE. It runs exactly the stored tool and arguments (re-hashed and compared
first) and asks the policy again, which still denies on kill switch, spend cap,
rate limit, disabled tool or a missing step-up.

Audit (P3.1 LOW-6): when an `audit` sink is wired, every decision is written
to `policy_audit` BEFORE the tool runs; if that write fails, nothing runs.

Canary (P4.4, ADR-0023): with a `canary` screen wired, the tool name and
arguments of every call (proposed AND approved) are checked before the policy;
a planted canary in any form means `deny.canary` and the tool never runs (the
guard writes the audit row and raises the alert). A screen error also denies.

`Risk`, `ProposedCall`, `Decision`, `Policy` and `DenyAllWritesPolicy` live in
`src.policy` and are re-exported here for existing importers.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from src.approvals.canonical import PayloadError, payload_sha256
from src.approvals.card import TARGET_KEY
from src.approvals.types import (
    TOOL_CALL_ACTION,
    ApprovalAsk,
    AskOutcome,
    AuditRecord,
    ConsumedApproval,
    thaw,
)
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
    approval: str = ""  # AskOutcome value when a requester was asked ("" = none)


class ApprovalRequester(Protocol):
    """Stores a `require_approval` call and asks the user. Never runs it."""

    async def request(self, ask: ApprovalAsk) -> AskOutcome: ...


class AuditSink(Protocol):
    async def audit(self, rec: AuditRecord) -> None: ...


class OutboundScreen(Protocol):
    """`src.canary.guard.CanaryGuard`: True = the text may leave."""

    async def screen(
        self, text: str, *, sink: str, tool: str | None = None, stream: bool = False
    ) -> bool: ...


CANARY_DENIED = "Denied: the arguments contain protected internal data (deny.canary)."


_ASK_TEXT: dict[AskOutcome, str] = {
    AskOutcome.REQUESTED: (
        "The user was sent an approval card with these exact arguments. The call has NOT "
        "run; it runs only if the user approves it. Do not repeat the call."
    ),
    AskOutcome.DUPLICATE: (
        "An approval card for this exact call is already waiting for the user. The call "
        "has NOT run. Do not repeat the call."
    ),
    AskOutcome.TOO_MANY: "Denied: too many approvals are already waiting for the user.",
    AskOutcome.INVALID: "Denied: these arguments cannot be shown to the user for approval.",
    AskOutcome.FAILED: "Denied: the approval card could not be sent.",
    AskOutcome.UNAVAILABLE: (
        "Denied: this action needs an authenticator step-up, which is not configured."
    ),
}


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
        approvals: ApprovalRequester | None = None,
        audit: AuditSink | None = None,
        canary: OutboundScreen | None = None,
    ) -> None:
        self._tools = dict(tools)
        self._canary = canary
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
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
        self,
        tool: LoopTool | None,
        name: str,
        exposed: bool,
        tainted: bool,
        approval: ConsumedApproval | None = None,
    ) -> PolicyContext:
        risk = tool.risk if tool is not None else None
        # The ONLY source of a confirmed context: a sealed ConsumedApproval
        # (pinned by tests/eval/invariants/test_inv5_approvals.py).
        confirmed = isinstance(approval, ConsumedApproval)
        stepped_up = confirmed and approval is not None and approval.step_up_verified is True
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
            user_confirmed=confirmed,
            step_up_confirmed=stepped_up,
        )

    async def _record(self, rec: AuditRecord) -> bool:
        """True when the decision is durably audited (or no sink is wired)."""
        if self._audit is None:
            return True
        try:
            await self._audit.audit(rec)
        except Exception as exc:  # noqa: BLE001 - unaudited decisions never run
            log.warning("policy audit write failed err=%s", type(exc).__name__)
            return False
        return True

    async def _args_clean(self, name: object, args: object) -> bool:
        """Canary screen of the tool name + arguments. No screen wired = clean."""
        if self._canary is None:
            return True
        tool = safe_name(str(name))
        try:
            blob = f"{name}\n" + json.dumps(args, default=str, ensure_ascii=False, sort_keys=True)
            got = await self._canary.screen(blob, sink="tool_args", tool=tool)
            if got is True:
                # Stream check over the argument VALUES only, so pieces spread
                # over several calls are seen side by side (keys would split them).
                got = await self._canary.screen(
                    "".join(_leaf_strings(args)), sink="tool_args", tool=tool, stream=True
                )
        except Exception as exc:  # noqa: BLE001 - fail closed
            log.warning("canary screen failed err=%s", type(exc).__name__)
            return False
        return got is True

    def _canary_denied(self, name: str, tainted: bool) -> DispatchResult:
        log.info(
            "dispatch decision tool=%s verdict=deny rule_id=deny.canary executed=no",
            safe_name(name),
        )
        return DispatchResult(name, "denied", CANARY_DENIED, rule_id="deny.canary", tainted=tainted)

    async def dispatch(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        exposed: frozenset[str],
        tainted: bool = False,
    ) -> DispatchResult:
        tainted = tainted is not False  # fail closed: only an explicit False is clean
        if not await self._args_clean(name, args):
            return self._canary_denied(name, tainted)
        if isinstance(args, Mapping) and TARGET_KEY in args:
            # P4.1: `_target` is the approval card's code-written target line,
            # hidden from the card's argument list; only a tool's
            # `describe_args` may set it, never the model (card spoofing).
            args = {k: v for k, v in args.items() if k != TARGET_KEY}
        tool = self._tools.get(name) if isinstance(name, str) else None
        is_exposed = tool is not None and name in exposed
        if tool is not None:
            # Optional offline hook (P3.5): e.g. turn an opaque handle into the real
            # id so the policy, card and stored payload all see the real value.
            resolver = getattr(tool, "resolve_args", None)
            if callable(resolver):
                rejected: DispatchResult | None = None
                try:
                    args = await resolver(args)
                except ToolError as e:
                    msg = str(e)[:MAX_TOOL_ERROR_CHARS]
                    rejected = DispatchResult(
                        name, "error", f"Tool error: {msg}", untrusted=True, rule_id="tool.args"
                    )
                except Exception:  # noqa: BLE001 - fail closed
                    rejected = DispatchResult(
                        name, "error", "Tool error: failed.", rule_id="tool.args"
                    )
                if rejected is not None:
                    return await self._reject_args(name, tool, args, rejected, is_exposed, tainted)
        call = ProposedCall(tool=name, args=args, risk=tool.risk if tool is not None else None)
        ctx = await self._safe_context(tool, name, is_exposed, tainted)
        decision = self._decide(call, ctx)
        res = await self._conclude(name, tool, args, decision, is_exposed, tainted, "decided")
        if res.needs_approval and decision is not None and self._approvals is not None:
            res = await self._ask(name, args, decision, tainted, res)
        return replace(res, tainted=tainted)

    async def _reject_args(
        self,
        name: str,
        tool: LoopTool,
        args: Mapping[str, Any],
        rejected: DispatchResult,
        is_exposed: bool,
        tainted: bool,
    ) -> DispatchResult:
        """A `resolve_args` refusal is audited (LOW-7), and a mode/scope/exposure
        denial still takes precedence: the policy decides on the unresolved call
        first, and only a call it would have let through gets `tool.args`."""
        call = ProposedCall(tool=name, args=args, risk=tool.risk)
        decision = self._decide(call, await self._safe_context(tool, name, is_exposed, tainted))
        if decision is None or decision.verdict == "deny" or not is_exposed:
            res = await self._conclude(name, tool, args, decision, False, tainted, "decided")
            return replace(res, tainted=tainted)
        audited = await self._record(
            AuditRecord(
                kind="decision",
                event="args_rejected",
                tool=safe_name(name),
                rule_id="tool.args",
                outcome="error",
                tainted=tainted,
                step_up=False,
                executed=False,
            )
        )
        if not audited:
            return DispatchResult(
                name,
                "denied",
                "Denied: audit unavailable.",
                rule_id="deny.audit_unavailable",
                tainted=tainted,
            )
        return replace(rejected, tainted=tainted)

    async def execute_approved(self, approval: ConsumedApproval) -> DispatchResult:
        """Run a call the user approved. `approval` must be a sealed
        `ConsumedApproval` (already consumed in the database). The stored
        arguments are re-hashed and must match; the policy decides again with
        `user_confirmed=True` (and `step_up_confirmed` only if the TOTP step-up
        was verified for this row). Tainted is forced True: the call was
        proposed by a model and the user's tap does not clean it."""
        if not isinstance(approval, ConsumedApproval) or approval.action != TOOL_CALL_ACTION:
            log.warning("execute_approved refused: not a consumed tool-call approval")
            return DispatchResult("?", "denied", "Denied: not an approval.", rule_id="deny.error")
        name = approval.tool
        args = thaw(approval.payload)
        try:
            again = payload_sha256(name, approval.action, args)
        except PayloadError:
            again = ""
        if not hmac.compare_digest(again.encode(), approval.payload_sha256.encode()):
            log.error("execute_approved refused: payload hash mismatch tool=%s", safe_name(name))
            return DispatchResult(
                name, "denied", "Denied: approval mismatch.", rule_id="deny.approval_mismatch"
            )
        if not await self._args_clean(name, args):
            return self._canary_denied(name, True)
        tool = self._tools.get(name)
        exists = tool is not None
        call = ProposedCall(tool=name, args=args, risk=tool.risk if tool is not None else None)
        ctx = await self._safe_context(tool, name, exists, True, approval)
        decision = self._decide(call, ctx)
        res = await self._conclude(
            name, tool, args, decision, exists, True, "decided_confirmed", approval
        )
        return replace(res, tainted=True)

    async def _safe_context(
        self,
        tool: LoopTool | None,
        name: str,
        exposed: bool,
        tainted: bool,
        approval: ConsumedApproval | None = None,
    ) -> PolicyContext | None:
        try:
            return await self._context(tool, name, exposed, tainted, approval)
        except Exception:  # noqa: BLE001 - fail closed
            return None

    def _decide(self, call: ProposedCall, ctx: PolicyContext | None) -> Decision | None:
        if ctx is None:
            return None
        try:
            decision = self._policy.decide(call, ctx)
            if not isinstance(decision, Decision):
                raise TypeError("policy returned a non-Decision")
        except Exception:  # noqa: BLE001 - fail closed
            return None
        return decision

    async def _conclude(
        self,
        name: str,
        tool: LoopTool | None,
        args: Mapping[str, Any],
        decision: Decision | None,
        is_exposed: bool,
        tainted: bool,
        event: str,
        approval: ConsumedApproval | None = None,
    ) -> DispatchResult:
        verdict = decision.verdict if decision is not None else "deny"
        rule_id = decision.rule_id if decision is not None else "deny.error"
        step_up = decision is not None and decision.step_up is True
        runnable = verdict == "allow" and is_exposed and tool is not None
        audited = await self._record(
            AuditRecord(
                kind="decision",
                event=event,
                tool=safe_name(name),
                rule_id=safe_name(rule_id),
                outcome=verdict,
                tainted=tainted,
                step_up=step_up,
                executed=runnable,
                action_id=approval.action_id if approval is not None else None,
            )
        )
        runnable = runnable and audited
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
            return DispatchResult(name, "denied", "Denied: policy error.", rule_id="deny.error")
        if not audited:
            return DispatchResult(
                name, "denied", "Denied: audit unavailable.", rule_id="deny.audit_unavailable"
            )
        if not runnable or tool is None:
            return _denied(name, decision, step_up)
        self._counter.record(name)
        return await self._run(name, tool, args, rule_id)

    async def _ask(
        self,
        name: str,
        args: Mapping[str, Any],
        decision: Decision,
        tainted: bool,
        res: DispatchResult,
    ) -> DispatchResult:
        requester = self._approvals
        outcome = AskOutcome.FAILED
        tool = self._tools.get(name)
        describer = getattr(tool, "describe_args", None)
        if callable(describer):
            # Optional online hook, reached only after the policy said
            # require_approval: lets the card show what the target really is.
            try:
                args = await describer(args)
            except Exception as exc:  # noqa: BLE001 - the card still shows the raw arguments
                log.warning(
                    "describe_args failed tool=%s err=%s", safe_name(name), type(exc).__name__
                )
        if requester is not None:
            ask = ApprovalAsk(
                tool=name, args=args, step_up=res.step_up, tainted=tainted, rule_id=decision.rule_id
            )
            try:
                got = await requester.request(ask)
                outcome = got if isinstance(got, AskOutcome) else AskOutcome.FAILED
            except Exception as exc:  # noqa: BLE001 - fail closed: still not run
                log.warning(
                    "approval request failed tool=%s err=%s", safe_name(name), type(exc).__name__
                )
        text = f"Approval required by policy ({decision.rule_id}). {_ASK_TEXT[outcome]}"
        return replace(res, content=text, approval=outcome.value)

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


def _leaf_strings(value: object, depth: int = 0) -> list[str]:
    """String and number leaves of tool arguments, in key order (bounded depth;
    anything deeper is stringified whole, so nothing is skipped)."""
    if depth > 16:
        return [str(value)]
    if isinstance(value, Mapping):
        return [s for k in sorted(value, key=str) for s in _leaf_strings(value[k], depth + 1)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _leaf_strings(v, depth + 1)]
    return [] if value is None else [str(value)]


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
