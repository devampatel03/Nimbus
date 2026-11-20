# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""The deterministic policy engine (ADR-0009, INV-5).

`PolicyEngine.decide(call, ctx)` normalises its inputs into `Facts` (failing
closed on anything of the wrong type), then walks `RULES` in order: the first
row whose predicate matches gives the verdict. A `require_approval` verdict is
lifted to `allow` only by a consumed user approval for this payload, plus the
TOTP step-up when the row demands it (both P3.2; the P3.1 dispatcher never sets
them). Any exception anywhere is a `deny.error`: `decide` never raises.

Pure: no I/O, no clock, no randomness, no logging (the dispatcher logs one line
per decision). The arguments of the call are never read, so model-chosen
values cannot influence the verdict.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from src.policy.config import PolicyConfig
from src.policy.registry import ToolRule
from src.policy.types import (
    Decision,
    PolicyContext,
    PolicyMode,
    ProposedCall,
    Risk,
    Verdict,
    allow_decision,
    deny_decision,
    make_decision,
)

# A name that looks like sending mail is denied before the registry is even
# consulted (R6: drafts only, no send path, ever).
_SEND_MARKERS = ("send",)
_MAIL_MARKERS = ("mail",)


@dataclass(frozen=True)
class Facts:
    """Normalised, fail-closed view of (call, ctx, config)."""

    malformed: bool
    send_mail: bool
    rule: ToolRule | None
    risk: Risk | None
    risk_known: bool  # call.risk is a Risk member
    risk_missing: bool  # call.risk is None: the dispatcher found no such tool
    exposed: bool
    disabled: bool
    scope_enabled: bool
    mode: PolicyMode | None
    calls_last_hour: int | None
    tainted: bool
    destructive: bool
    external: bool
    external_recipient: bool
    confirmed: bool
    step_up_confirmed: bool


@dataclass(frozen=True)
class Rule:
    rule_id: str
    reason: str
    outcome: Verdict
    when: Callable[[Facts], bool]
    step_up: Callable[[Facts], bool] | None = None


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _is_send_mail(name: str) -> bool:
    n = _norm(name)
    return any(s in n for s in _SEND_MARKERS) and any(m in n for m in _MAIL_MARKERS)


def _facts(call: object, ctx: object, cfg: PolicyConfig) -> Facts:
    if (
        not isinstance(call, ProposedCall)
        or not isinstance(ctx, PolicyContext)
        or not isinstance(call.tool, str)
        or not isinstance(call.args, Mapping)
    ):
        return _MALFORMED
    raw_rule = cfg.rules.get(call.tool)
    rule = raw_rule if isinstance(raw_rule, ToolRule) else None
    risk_known = isinstance(call.risk, Risk)
    risk = call.risk if risk_known else None
    calls = ctx.calls_last_hour
    scopes = ctx.scopes_enabled
    return Facts(
        malformed=False,
        send_mail=_is_send_mail(call.tool),
        rule=rule,
        risk=risk,
        risk_known=risk_known,
        risk_missing=call.risk is None,
        exposed=ctx.exposed is True,
        disabled=call.tool in cfg.disabled_tools,
        scope_enabled=(rule is not None and isinstance(scopes, frozenset) and rule.scope in scopes),
        mode=ctx.mode if isinstance(ctx.mode, PolicyMode) else None,
        calls_last_hour=calls if type(calls) is int and calls >= 0 else None,
        # Fail closed: only an explicit False is clean / not destructive / not external.
        tainted=ctx.tainted is not False,
        destructive=(
            risk is Risk.DESTRUCTIVE
            or (rule is not None and rule.destructive)
            or ctx.destructive is not False
        ),
        external=risk is Risk.WRITE_EXTERNAL or ctx.external_recipient is not False,
        external_recipient=ctx.external_recipient is not False,
        # Fail closed the other way: only an explicit True counts as an approval.
        confirmed=ctx.user_confirmed is True,
        step_up_confirmed=ctx.step_up_confirmed is True,
    )


_MALFORMED = Facts(
    malformed=True,
    send_mail=False,
    rule=None,
    risk=None,
    risk_known=False,
    risk_missing=True,
    exposed=False,
    disabled=True,
    scope_enabled=False,
    mode=None,
    calls_last_hour=None,
    tainted=True,
    destructive=True,
    external=True,
    external_recipient=True,
    confirmed=False,
    step_up_confirmed=False,
)


def _rate_exceeded(f: Facts) -> bool:
    if f.calls_last_hour is None or f.rule is None:
        return True
    return f.calls_last_hour >= f.rule.max_calls_per_hour


def _killed_blocks(f: Facts) -> bool:
    if f.mode is not PolicyMode.KILLED:
        return False
    return f.risk is not Risk.READ or f.rule is None or f.rule.external_service


# First matching row wins (ADR-0009). Rows 1-4 of the ADR are expanded into
# the explicit deny rows below; rows 5-9 are the effect rows.
RULES: tuple[Rule, ...] = (
    Rule("deny.malformed", "malformed call", "deny", lambda f: f.malformed),
    Rule("deny.gmail_send", "sending mail is never allowed", "deny", lambda f: f.send_mail),
    Rule(
        "deny.unknown_tool",
        "unknown tool",
        "deny",
        lambda f: f.rule is None or f.risk_missing,
    ),
    Rule("deny.unknown_risk", "unknown risk class", "deny", lambda f: not f.risk_known),
    Rule(
        "deny.risk_mismatch",
        "risk differs from policy",
        "deny",
        lambda f: f.rule is None or f.risk is not f.rule.risk,
    ),
    Rule("deny.unexposed", "tool not offered this turn", "deny", lambda f: not f.exposed),
    Rule("deny.tool_disabled", "tool disabled by config", "deny", lambda f: f.disabled),
    Rule("deny.scope_disabled", "scope not enabled", "deny", lambda f: not f.scope_enabled),
    Rule("deny.unknown_mode", "unknown mode", "deny", lambda f: f.mode is None),
    Rule("deny.killed", "kill switch engaged", "deny", _killed_blocks),
    Rule(
        "deny.read_only",
        "read-only mode (spend cap)",
        "deny",
        lambda f: f.mode is PolicyMode.READ_ONLY and f.risk is not Risk.READ,
    ),
    Rule("deny.rate_limit", "rate limit reached", "deny", _rate_exceeded),
    Rule(
        "approval.destructive",
        "destructive action needs approval and step-up",
        "require_approval",
        lambda f: f.destructive,
        step_up=lambda f: True,
    ),
    Rule(
        "approval.write_external",
        "external effect needs approval",
        "require_approval",
        lambda f: f.external,
        step_up=lambda f: f.external_recipient,
    ),
    Rule("allow.read", "read-only", "allow", lambda f: f.risk is Risk.READ),
    Rule(
        "allow.write_internal",
        "local write, untainted turn",
        "allow",
        lambda f: f.risk is Risk.WRITE_INTERNAL and not f.tainted,
    ),
    Rule(
        "approval.write_internal_tainted",
        "local write in a tainted turn needs approval",
        "require_approval",
        lambda f: f.risk is Risk.WRITE_INTERNAL and f.tainted,
    ),
    Rule("deny.no_rule", "no rule matched", "deny", lambda f: True),
)

_FALLBACK = ("deny.no_rule", "no rule matched")


def _step_up_satisfied(f: Facts, step_up: bool) -> bool:
    return f.step_up_confirmed or not step_up


def _evaluate(f: Facts) -> Decision:
    for row in RULES:
        if not row.when(f):
            continue
        step_up = bool(row.step_up(f)) if row.step_up is not None else False
        if row.outcome == "require_approval" and f.confirmed and _step_up_satisfied(f, step_up):
            return allow_decision("allow.confirmed", "user approved", step_up=step_up)
        return make_decision(row.outcome, row.rule_id, row.reason, step_up=step_up)
    return deny_decision(*_FALLBACK)


class PolicyEngine:
    """The production `Policy` (ADR-0009)."""

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self._cfg = config if config is not None else PolicyConfig()

    def decide(self, call: ProposedCall, ctx: PolicyContext) -> Decision:
        try:
            return _evaluate(_facts(call, ctx, self._cfg))
        except Exception:  # noqa: BLE001 - fail closed, never raise
            return deny_decision("deny.error", "policy error")

    def offerable(self, tool: str, scopes_enabled: frozenset[str]) -> bool:
        """Whether the loop may OFFER `tool` to the model: the same table
        `decide` uses has a rule for it and its scope is enabled. Restrictive
        only: True grants nothing (every call is still decided); False hides a
        tool `decide` would deny anyway (`deny.unknown_tool` /
        `deny.scope_disabled`).

        `disabled_tools` are still offered (and denied `deny.tool_disabled`):
        the protected `tests/eval/invariants/test_inv5_policy_engine.py` pins
        that verdict for a model-called `tasks_create`. Hiding them needs that
        test changed with permission (HUMAN_TODO H29)."""
        try:
            if not isinstance(tool, str) or not isinstance(scopes_enabled, frozenset):
                return False
            rule = self._cfg.rules.get(tool)
            return isinstance(rule, ToolRule) and rule.scope in scopes_enabled
        except Exception:  # noqa: BLE001 - fail closed
            return False


class DenyAllWritesPolicy:
    """Strict fallback (P2.5a interim; kept for tests): READ is allowed when the
    tool is exposed; every write, external effect and unknown tool is denied."""

    def decide(self, call: ProposedCall, ctx: PolicyContext | None = None) -> Decision:
        if call.risk is None or (ctx is not None and ctx.exposed is not True):
            return deny_decision("deny.unknown_tool", "unknown tool")
        if call.risk is Risk.READ:
            return allow_decision("allow.read", "read-only")
        return deny_decision("deny.write_interim", "writes are disabled for now")
