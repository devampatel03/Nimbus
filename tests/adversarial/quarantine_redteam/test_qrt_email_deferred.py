# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.1 red team, category email_instruction_injection: DEFERRED.

There is no Gmail read tool yet: P3.6 (Gmail, drafts only) is blocked on human
gate H16 (ADR-0007 Proposed), so no email can reach a turn today. The quarantine
side already exists (`SourceKind.EMAIL` -> `EmailDigest`) and is exercised by
`tests/adversarial/lanes/test_lanes_injection.py`; the instruction-injection
vectors themselves run in the other categories here (`instruction_override`,
`send_and_share`).

When P3.6 lands, replace the skip with the `_qrt_harness.VECTORS` matrix over
the real Gmail read tool (shape: From / Subject / body, plus HTML parts and
attachments' file names), as in `test_qrt_doc.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from src.policy import TOOL_RULES

DEFERRED_REASON = (
    "email category deferred: P3.6 Gmail read tool is blocked on H16 (ADR-0007 "
    "Proposed); no email source can reach a turn yet"
)


@pytest.mark.skip(reason=DEFERRED_REASON)
def test_email_category_placeholder() -> None:
    raise AssertionError("placeholder: implement with P3.6")


def test_no_gmail_tool_exists_while_the_category_is_deferred() -> None:
    """Fails the day a Gmail tool gets a policy row, so this file cannot stay
    a silent skip once email can actually reach a turn."""
    assert not [name for name in TOOL_RULES if "mail" in name.lower()]


# Name-independent guards: whatever a future mail tool is called, producing
# email-sourced untrusted text or using a Gmail scope / host trips one of these.
SRC = Path(__file__).parents[3] / "src"
# Where the email source kind and its schema are DEFINED (the quarantine lane).
EMAIL_KIND_ALLOWED = {"src/lanes/data.py", "src/lanes/schemas.py"}
# Where Gmail scopes and the Gmail host are DEFINED (allow-lists, consent).
GMAIL_ALLOWED = {"src/integrations/google/auth.py", "src/net/policy.py"}
GMAIL_NAMES = {"SCOPE_GMAIL_READONLY", "SCOPE_GMAIL_COMPOSE"}
GMAIL_LITERALS = ("gmail.googleapis.com", "auth/gmail", "/gmail/v")


def _modules() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"))))
    return out


def _email_kind_uses(tree: ast.Module) -> list[int]:
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == "EMAIL":
            hits.append(n.lineno)  # SourceKind.EMAIL (any alias of the enum)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name | ast.Attribute):
            fname = n.func.id if isinstance(n.func, ast.Name) else n.func.attr
            if fname == "SourceKind" and any(
                isinstance(a, ast.Constant) and a.value == "email" for a in n.args
            ):
                hits.append(n.lineno)  # SourceKind("email")
    return hits


def _gmail_uses(tree: ast.Module) -> list[int]:
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name | ast.alias):
            name = n.id if isinstance(n, ast.Name) else n.name
            if name in GMAIL_NAMES:
                hits.append(getattr(n, "lineno", 0))
        elif isinstance(n, ast.Attribute) and n.attr in (*GMAIL_NAMES, "GMAIL"):
            hits.append(n.lineno)  # auth.SCOPE_GMAIL_*, Service.GMAIL
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            if any(lit in n.value for lit in GMAIL_LITERALS):
                hits.append(n.lineno)
    return hits


def test_nothing_produces_email_sourced_untrusted_text() -> None:
    bad = {
        rel: lines
        for rel, tree in _modules()
        if rel not in EMAIL_KIND_ALLOWED and (lines := _email_kind_uses(tree))
    }
    assert bad == {}, f"email source reaches code outside src/lanes: {bad} (un-defer this file)"


def test_nothing_uses_a_gmail_scope_or_host() -> None:
    bad = {
        rel: lines
        for rel, tree in _modules()
        if rel not in GMAIL_ALLOWED and (lines := _gmail_uses(tree))
    }
    assert bad == {}, f"Gmail scope/host used outside its definitions: {bad} (un-defer this file)"


@pytest.mark.parametrize(
    ("snippet", "check"),
    [
        ("x = UntrustedText(t, source=SourceKind.EMAIL)", _email_kind_uses),
        ("import src.lanes.data as d; k = d.SourceKind.EMAIL", _email_kind_uses),
        ("k = SourceKind('email')", _email_kind_uses),
        ("from src.integrations.google.auth import SCOPE_GMAIL_READONLY", _gmail_uses),
        ("s = auth.SCOPE_GMAIL_READONLY", _gmail_uses),
        ("svc = Service.GMAIL", _gmail_uses),
        ("u = 'https://www.googleapis.com/auth/gmail.readonly'", _gmail_uses),
        ("p = '/gmail/v1/users/me/messages'", _gmail_uses),
    ],
)
def test_guards_catch_a_renamed_mail_tool(snippet: str, check: Any) -> None:
    """Mutation proof: each producer shape is caught, whatever the tool's name."""
    assert check(ast.parse(snippet))
