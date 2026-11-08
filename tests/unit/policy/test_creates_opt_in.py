# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Human opt-in for `tasks_create` / `reminders_create` (P3.3-enable, INV-5).

* The DEFAULT `PolicyConfig` is unchanged: both creates stay `deny.tool_disabled`
  (also pinned by the protected eval test).
* `config_with_creates(True)` lifts exactly those two names and nothing else;
  the ADR-0009 matrix then decides: allowed in an untainted turn, approval card
  in a tainted one. Deletes are untouched (destructive: approval + step-up).
* `POLICY_ENABLE_CREATES` is a hidden operator setting: default false, strict
  `true`/`false` only, frozen, value never echoed in an error.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.app.policy_settings import PolicySettings
from src.policy import (
    LOCAL_SCOPES,
    OPT_IN_CREATES,
    PENDING_IDEMPOTENCY,
    PolicyConfig,
    PolicyContext,
    PolicyEngine,
    PolicyMode,
    ProposedCall,
    Risk,
    config_with_creates,
)

CREATES = ("tasks_create", "reminders_create")
DELETES = ("tasks_delete", "reminders_delete")


def ctx(**kw: Any) -> PolicyContext:
    base: dict[str, Any] = {
        "exposed": True,
        "tainted": False,
        "mode": PolicyMode.NORMAL,
        "scopes_enabled": LOCAL_SCOPES,
        "calls_last_hour": 0,
        "external_recipient": False,
        "destructive": False,
        "quiet_hours": False,
    }
    base.update(kw)
    return PolicyContext(**base)


def decide(cfg: PolicyConfig, tool: str, **kw: Any) -> tuple[str, str, bool]:
    d = PolicyEngine(cfg).decide(ProposedCall(tool, {}, Risk.WRITE_INTERNAL), ctx(**kw))
    return d.verdict, d.rule_id, d.step_up


def test_opt_in_set_is_exactly_the_two_creates() -> None:
    assert OPT_IN_CREATES == frozenset(CREATES)
    assert OPT_IN_CREATES <= PENDING_IDEMPOTENCY


def test_off_is_the_default_config() -> None:
    assert config_with_creates(False) == PolicyConfig()
    assert config_with_creates(False).disabled_tools == PENDING_IDEMPOTENCY


def test_on_lifts_only_the_two_creates() -> None:
    cfg = config_with_creates(True)
    assert cfg.disabled_tools == PENDING_IDEMPOTENCY - OPT_IN_CREATES
    assert cfg.rules is PolicyConfig().rules  # no rule is changed or added


@pytest.mark.parametrize("value", [1, "true", None, "yes"])
def test_only_a_real_bool_switches(value: Any) -> None:
    with pytest.raises(TypeError):
        config_with_creates(value)


# ADR-0009 matrix for the creates, both configs: (enabled, tainted) -> outcome.
MATRIX = [
    (False, False, ("deny", "deny.tool_disabled", False)),
    (False, True, ("deny", "deny.tool_disabled", False)),
    (True, False, ("allow", "allow.write_internal", False)),
    (True, True, ("require_approval", "approval.write_internal_tainted", False)),
]


@pytest.mark.parametrize("tool", CREATES)
@pytest.mark.parametrize(("enabled", "tainted", "want"), MATRIX)
def test_create_matrix(tool: str, enabled: bool, tainted: bool, want: tuple[Any, ...]) -> None:
    assert decide(config_with_creates(enabled), tool, tainted=tainted) == want


@pytest.mark.parametrize("tool", CREATES)
def test_enabled_create_still_fails_closed_on_every_gate(tool: str) -> None:
    cfg = config_with_creates(True)
    assert decide(cfg, tool, exposed=False)[1] == "deny.unexposed"
    assert decide(cfg, tool, scopes_enabled=frozenset())[1] == "deny.scope_disabled"
    assert decide(cfg, tool, mode=PolicyMode.KILLED)[1] == "deny.killed"
    assert decide(cfg, tool, mode=PolicyMode.READ_ONLY)[1] == "deny.read_only"
    assert decide(cfg, tool, calls_last_hour=10_000)[1] == "deny.rate_limit"
    assert decide(cfg, tool, tainted=None)[1] == "approval.write_internal_tainted"
    d = PolicyEngine(cfg).decide(ProposedCall(tool, {}, Risk.READ), ctx())
    assert d.rule_id == "deny.risk_mismatch"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("tool", DELETES)
def test_deletes_unchanged_by_the_opt_in(enabled: bool, tool: str) -> None:
    assert decide(config_with_creates(enabled), tool) == (
        "require_approval",
        "approval.destructive",
        True,
    )


# ----------------------------------------------------------------- the setting


def test_setting_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLICY_ENABLE_CREATES", raising=False)
    assert PolicySettings(_env_file=None).enable_creates is False  # type: ignore[call-arg]


@pytest.mark.parametrize(("raw", "want"), [("true", True), ("TRUE", True), ("false", False)])
def test_setting_reads_strict_true_false(
    monkeypatch: pytest.MonkeyPatch, raw: str, want: bool
) -> None:
    monkeypatch.setenv("POLICY_ENABLE_CREATES", raw)
    assert PolicySettings(_env_file=None).enable_creates is want  # type: ignore[call-arg]


@pytest.mark.parametrize("raw", ["1", "yes", "on", "", " true", "tru", "0", "y", "t"])
def test_setting_refuses_anything_else(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("POLICY_ENABLE_CREATES", raw)
    with pytest.raises(ValidationError):
        PolicySettings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("value", [1, 0, None, 1.0])
def test_setting_refuses_non_bool_init_values(value: Any) -> None:
    with pytest.raises(ValidationError):
        PolicySettings(_env_file=None, enable_creates=value)  # type: ignore[call-arg]


def test_a_refused_value_is_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_ENABLE_CREATES", "yesZZQ7")
    with pytest.raises(ValidationError) as exc:
        PolicySettings(_env_file=None)  # type: ignore[call-arg]
    assert "ZZQ7" not in str(exc.value) and "ZZQ7" not in repr(exc.value)


def test_setting_is_frozen() -> None:
    s = PolicySettings(_env_file=None, enable_creates=False)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        s.enable_creates = True  # type: ignore[misc]


def test_setting_maps_to_the_config() -> None:
    on = PolicySettings(_env_file=None, enable_creates=True)  # type: ignore[call-arg]
    off = PolicySettings(_env_file=None, enable_creates=False)  # type: ignore[call-arg]
    assert on.policy_config() == config_with_creates(True)
    assert off.policy_config() == PolicyConfig()
