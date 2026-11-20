# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""INV-5 regression (P2.5b): LLM output never grants a permission.

Static scan of `src/`:
  1. no class declares a grant-shaped field (`approved`, `allowed`, ...) except
     the code-produced `Decision.verdict`;
  2. no code reads such an attribute/key off a model-output-looking object or
     passes one as a keyword;
  3. `Policy.decide` takes exactly (`ProposedCall`, `PolicyContext`): the call's
     fields are exactly (tool, args, risk) and the context's fields are exactly
     the pinned code-derived facts (P3.1; stricter than the old one-arg form);
  4. model calls (`.chat/.structured`) happen only in the loop, the memory
     extractor (unreviewed rows) and the friction check (advisory text).
The scan root is a parameter so the mutation proof can aim it at a throwaway tree.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from src.policy import Decision, Policy, PolicyContext, PolicyMode, ProposedCall, Risk

SRC = Path(__file__).resolve().parents[3] / "src"
GRANT_NAMES = {
    "approved",
    "is_approved",
    "approve",
    "allowed",
    "is_allowed",
    "permitted",
    "authorized",
    "authorised",
    "granted",
    "permission",
    "verdict",
}
# The only place a grant-shaped field may exist: the code-produced Decision
# (moved from src/orchestrator/dispatch.py to src/policy/types.py in P3.1).
ALLOWED_DECLARATIONS = {("src/policy/types.py", "verdict")}
# Every PolicyContext field is a fact computed by code (dispatcher, guard,
# counters, P3.2 consumed approvals), never a value read from model output.
CTX_FIELDS = {
    "exposed": "bool",
    "tainted": "bool",
    "mode": "PolicyMode",
    "scopes_enabled": "frozenset[str]",
    "calls_last_hour": "int",
    "external_recipient": "bool",
    "destructive": "bool",
    "quiet_hours": "bool",
    "user_confirmed": "bool",
    "step_up_confirmed": "bool",
}
MODEL_OUTPUT_NAMES = {"res", "resp", "response", "result", "out", "output", "completion", "reply"}


def _rel(p: Path, root: Path) -> str:
    return p.relative_to(root.parent).as_posix()


def _is_str_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def scan(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = _rel(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for stmt in n.body:
                    if (
                        isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id.lower() in GRANT_NAMES
                        and (rel, stmt.target.id) not in ALLOWED_DECLARATIONS
                    ):
                        problems.append(f"{rel}:{stmt.lineno} grant-shaped field")
            if (
                isinstance(n, ast.Attribute)
                and n.attr.lower() in GRANT_NAMES
                and isinstance(n.value, ast.Name)
                and n.value.id in MODEL_OUTPUT_NAMES
            ):
                problems.append(f"{rel}:{n.lineno} reads .{n.attr} from {n.value.id}")
            if (
                isinstance(n, ast.Subscript)
                and isinstance(n.slice, ast.Constant)
                and isinstance(n.slice.value, str)
                and n.slice.value.lower() in GRANT_NAMES
            ):
                problems.append(f"{rel}:{n.lineno} reads key {n.slice.value!r}")
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"
                and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)
                and n.args[0].value.lower() in GRANT_NAMES
            ):
                problems.append(f"{rel}:{n.lineno} .get({n.args[0].value!r})")
            if (
                isinstance(n, ast.keyword)
                and n.arg
                and n.arg.lower() in GRANT_NAMES
                and (rel, n.arg) not in ALLOWED_DECLARATIONS
            ):
                problems.append(f"{rel} keyword {n.arg}=")
    return problems


def test_src_has_no_llm_granted_permission() -> None:
    assert scan(SRC) == []


def test_policy_interface_takes_a_proposed_call_and_code_derived_context() -> None:
    sig = inspect.signature(Policy.decide)
    assert list(sig.parameters) == ["self", "call", "ctx"]
    assert sig.parameters["call"].annotation == "ProposedCall"
    assert sig.parameters["ctx"].annotation == "PolicyContext"
    assert [f.name for f in dataclasses.fields(ProposedCall)] == ["tool", "args", "risk"]
    assert {f.name: f.type for f in dataclasses.fields(PolicyContext)} == CTX_FIELDS
    assert [f.name for f in dataclasses.fields(Decision)] == [
        "verdict",
        "rule_id",
        "reason",
        "step_up",
    ]
    # Approval facts default to "not approved"; everything else must be set.
    defaults = {
        f.name: f.default
        for f in dataclasses.fields(PolicyContext)
        if f.default is not dataclasses.MISSING
    }
    assert defaults == {"user_confirmed": False, "step_up_confirmed": False}
    assert PolicyContext.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_policy_context_cannot_be_built_from_model_shaped_extras() -> None:
    base = {
        "exposed": True,
        "tainted": False,
        "mode": PolicyMode.NORMAL,
        "scopes_enabled": frozenset(),
        "calls_last_hour": 0,
        "external_recipient": False,
        "destructive": False,
        "quiet_hours": False,
    }
    with pytest.raises(TypeError):
        PolicyContext(**base, approved=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        PolicyContext(True, False)  # type: ignore[misc]  # keyword-only
    assert ProposedCall("t", {}, Risk.READ).risk is Risk.READ


def test_model_calls_only_from_known_callers() -> None:
    callers: set[str] = set()
    for path in SRC.rglob("*.py"):
        rel = _rel(path, SRC)
        if rel.startswith("src/llm/"):
            continue
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"chat", "structured"}
            ):
                callers.add(rel)
    # extractor: writes unreviewed rows. agent_character: advisory friction text.
    # Neither output can reach a Policy or grant anything (checked by the scan above).
    # lanes/quarantine (P3.0): QUARANTINE lane, no tools; output is schema-validated
    # untrusted DATA (INV-6) and sets taint, so it can only tighten policy.
    allowed = {
        "src/orchestrator/loop.py",
        "src/memory/extractor.py",
        "src/personality/agent_character.py",
        "src/lanes/quarantine.py",
    }
    assert callers <= allowed, callers


@pytest.mark.parametrize(
    "snippet",
    [
        "class J(BaseModel):\n    approved: bool\n",
        "def f(res):\n    return res.approved\n",
        "def f(res):\n    return res['approved']\n",
        "def f(d):\n    return d.get('allowed')\n",
        "def f():\n    return Judgment(approved=True)\n",
    ],
)
def test_scanner_detects_each_grant_shape(tmp_path: Path, snippet: str) -> None:
    """Mutation proof: a throwaway module with each forbidden shape is flagged."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "evil.py").write_text(snippet, encoding="utf-8")
    assert scan(root), snippet


def test_scanner_passes_a_clean_tree(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "ok.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    assert scan(root) == []
