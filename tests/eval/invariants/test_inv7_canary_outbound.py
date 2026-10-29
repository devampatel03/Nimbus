# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-7 / INV-6 (P4.4, ADR-0023): planted canaries never leave the process.

Structural pins on the production wiring (`src/app/runtime.py::run_app`):
  1. no component is handed the raw `be.gateway`; only `wire_canary` sees it,
     and everything else gets the `CanaryGateway` it returns;
  2. the `Orchestrator` is built with `canary=` (plants + reply/tool-arg screen);
  3. `wire_canary` installs the egress outbound check and the Telegram screen
     (`set_outbound_screen`), with a `CanaryGateway` fallback for other gateways;
  4. the log scrubber gets the canary values and the canary matcher before
     anything else runs.
Behaviour: a compromised reply and a canary-bearing tool argument are blocked
end to end with the real guard.

The scanner takes the source text as a parameter so the mutation proof can
feed it a weakened copy.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.canary.gateway import CanaryGateway
from src.canary.guard import BLOCKED_REPLY, CanaryGuard
from src.canary.tokens import Site, generate_canaries
from src.orchestrator.dispatch import Dispatcher
from src.policy import Decision

RUNTIME = Path(__file__).resolve().parents[3] / "src" / "app" / "runtime.py"


def _func(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def wiring_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    out: list[str] = []
    run_app = _func(tree, "run_app")
    for node in ast.walk(run_app):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "gateway"
            and isinstance(node.value, ast.Name)
            and node.value.id == "be"
        ):
            out.append(f"run_app:{node.lineno} uses be.gateway directly")
    calls = [n for n in ast.walk(run_app) if isinstance(n, ast.Call)]
    names = [getattr(c.func, "id", getattr(c.func, "attr", "")) for c in calls]
    if "wire_canary" not in names:
        out.append("run_app does not call wire_canary")
    if "add_log_matchers" not in names:
        out.append("run_app does not add the canary log matcher")
    for c in calls:
        if getattr(c.func, "id", "") == "Orchestrator" and not any(
            k.arg == "canary" for k in c.keywords
        ):
            out.append(f"run_app:{c.lineno} builds the Orchestrator without canary=")
    wire = _func(tree, "wire_canary")
    wire_calls = [
        getattr(n.func, "attr", getattr(n.func, "id", ""))
        for n in ast.walk(wire)
        if isinstance(n, ast.Call)
    ]
    if "set_outbound_check" not in wire_calls:
        out.append("wire_canary does not install the egress outbound check")
    consts = [
        n.value for n in ast.walk(wire) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    if "set_outbound_screen" not in consts:
        out.append("wire_canary does not install the Telegram outbound screen")
    if "CanaryGateway" not in wire_calls:
        out.append("wire_canary has no CanaryGateway fallback")
    return out


def test_production_wiring_routes_everything_through_the_canary_guard() -> None:
    assert wiring_violations(RUNTIME.read_text(encoding="utf-8")) == []


def test_scanner_catches_a_weakened_runtime() -> None:
    src = RUNTIME.read_text(encoding="utf-8")
    weakened = src.replace("gateway=gateway,", "gateway=be.gateway,").replace("canary=canary,", "")
    assert weakened != src
    found = wiring_violations(weakened)
    assert any("be.gateway" in f for f in found)
    assert any("without canary=" in f for f in found)


class _Gw:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str, *, reply_to: object = None) -> list[object]:
        self.sent.append(text)
        return []


class _Allow:
    def decide(self, call: object, ctx: object) -> Decision:
        return Decision("allow", "allow.inv7", "test")


class _Tool:
    def __init__(self) -> None:
        from src.llm.types import ToolSpec
        from src.orchestrator.dispatch import Risk

        self.spec = ToolSpec(name="w", description="w")
        self.risk = Risk.WRITE_EXTERNAL
        self.ran = 0

    async def run(self, args: object) -> object:
        self.ran += 1
        raise AssertionError("must not run")


async def test_a_leaked_canary_is_blocked_on_telegram_and_in_tool_args() -> None:
    cs = generate_canaries()
    guard = CanaryGuard(cs)
    inner = _Gw()
    gw = CanaryGateway(inner, guard)  # type: ignore[arg-type]
    await gw.send_text("ok: " + " ".join(cs.value(Site.SYSTEM)).upper())
    assert inner.sent == [BLOCKED_REPLY]
    tool = _Tool()
    d = Dispatcher({"w": tool}, _Allow(), canary=guard)  # type: ignore[dict-item]
    res = await d.dispatch("w", {"t": cs.value(Site.MEMORY)[::-1]}, exposed=frozenset({"w"}))
    assert res.rule_id == "deny.canary" and tool.ran == 0
