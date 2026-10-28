# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The policy registry matches the discovered tools exactly, and contains no
generic web-fetch tool and no mail-send tool."""

from __future__ import annotations

import re

import pytest

from src.policy import LOCAL_SCOPES, TOOL_RULES, PolicyConfig, PolicyEngine, Risk, ToolRule
from src.policy.registry import DELETE_PER_HOUR, READ_PER_HOUR, WRITE_PER_HOUR
from src.tools.registry import ToolRegistry

GOOGLE_SCOPES = frozenset({"drive"})
FETCH_LIKE = re.compile(r"fetch|http|url|browse|web|scrape|curl|download|request", re.IGNORECASE)


def test_registry_covers_exactly_the_discovered_tools_with_the_same_risk() -> None:
    discovered = {t.spec.name: t.risk for t in ToolRegistry().loop_tools()}
    assert {k: v.risk for k, v in TOOL_RULES.items()} == discovered


def test_no_generic_web_fetch_tool_anywhere() -> None:
    names = set(TOOL_RULES) | {t.spec.name for t in ToolRegistry().loop_tools()}
    assert not [n for n in names if FETCH_LIKE.search(n)]


def test_no_mail_send_tool_and_the_engine_denies_one_if_added() -> None:
    assert not [n for n in TOOL_RULES if "send" in n.lower()]
    rules = dict(TOOL_RULES) | {"gmail_send": ToolRule(Risk.WRITE_EXTERNAL, "gmail", 99)}
    from src.policy import PolicyContext, PolicyMode, ProposedCall

    x = PolicyContext(
        exposed=True,
        tainted=False,
        mode=PolicyMode.NORMAL,
        scopes_enabled=frozenset({"gmail"}),
        calls_last_hour=0,
        external_recipient=False,
        destructive=False,
        quiet_hours=False,
        user_confirmed=True,
        step_up_confirmed=True,
    )
    d = PolicyEngine(PolicyConfig(rules=rules)).decide(
        ProposedCall("gmail_send", {}, Risk.WRITE_EXTERNAL), x
    )
    assert (d.verdict, d.rule_id) == ("deny", "deny.gmail_send")


def test_every_rule_has_a_local_scope_and_a_sane_limit() -> None:
    for name, rule in TOOL_RULES.items():
        if rule.scope in GOOGLE_SCOPES:
            assert rule.external_service is True, name
        else:
            assert rule.scope in LOCAL_SCOPES, name
            assert rule.external_service is False, name
        assert rule.max_calls_per_hour in {READ_PER_HOUR, WRITE_PER_HOUR, DELETE_PER_HOUR}
        want = READ_PER_HOUR if rule.risk is Risk.READ else WRITE_PER_HOUR
        if rule.destructive:
            want = DELETE_PER_HOUR
        assert rule.max_calls_per_hour == want, name


@pytest.mark.parametrize("name", ["tasks_delete", "reminders_delete"])
def test_deletes_are_marked_destructive(name: str) -> None:
    assert TOOL_RULES[name].destructive is True


def test_only_deletes_are_destructive() -> None:
    assert {n for n, r in TOOL_RULES.items() if r.destructive} == {
        "tasks_delete",
        "reminders_delete",
    }


def test_registry_is_read_only() -> None:
    with pytest.raises(TypeError):
        TOOL_RULES["x"] = ToolRule(Risk.READ, "tasks", 1)  # type: ignore[index]


def test_drive_tools_are_read_only_external_and_not_a_local_scope() -> None:
    for name in ("drive_search", "drive_read"):
        rule = TOOL_RULES[name]
        assert (rule.risk, rule.scope, rule.destructive) == (Risk.READ, "drive", False)
        assert rule.external_service is True
    assert "drive" not in LOCAL_SCOPES
    assert not [
        n for n in TOOL_RULES if n.startswith("drive_") and TOOL_RULES[n].risk is not Risk.READ
    ]
