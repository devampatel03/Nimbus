# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Human-edited policy configuration. The model can never change it.

`PENDING_IDEMPOTENCY` keeps `tasks_create` and `reminders_create` denied in
production (`deny.tool_disabled`): they stay off by default until a separate
enable task, which needs its own review. P3.3 has merged, but turning creation
on is a policy change, not part of it; a redelivered update or a retried model
call must not create duplicates.

The default stays OFF (pinned by the protected INV-5 eval test). The operator
opts in with `POLICY_ENABLE_CREATES=true` (`src.app.policy_settings`, read once
at boot from the environment, never from model output), which makes the app
build `config_with_creates(True)`: exactly `OPT_IN_CREATES` is lifted from the
deny list and the ADR-0009 matrix then decides (untainted turn: allowed;
tainted: approval card). Deletes are not affected. Idempotency is enforced by
the per-event dedupe indexes (`uq_reminder_source_ltitle_at`,
`uq_task_source_ltitle`, migration b2f6d4a8c1e7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from src.policy.registry import TOOL_RULES, ToolRule

PENDING_IDEMPOTENCY: frozenset[str] = frozenset({"tasks_create", "reminders_create"})
# The ONLY names the operator opt-in may lift. Never widened without review.
OPT_IN_CREATES: frozenset[str] = frozenset({"tasks_create", "reminders_create"})


@dataclass(frozen=True)
class PolicyConfig:
    rules: Mapping[str, ToolRule] = field(default=TOOL_RULES)
    disabled_tools: frozenset[str] = PENDING_IDEMPOTENCY

    def __post_init__(self) -> None:
        if not isinstance(self.disabled_tools, frozenset):
            raise TypeError("disabled_tools must be a frozenset")
        if not isinstance(self.rules, Mapping):
            raise TypeError("rules must be a mapping")


def config_with_creates(enabled: bool) -> PolicyConfig:
    """The production config. `enabled` comes from the human-edited
    `POLICY_ENABLE_CREATES` only; anything but a real bool is refused."""
    if type(enabled) is not bool:
        raise TypeError("enabled must be a bool")
    if not enabled:
        return PolicyConfig()
    return PolicyConfig(disabled_tools=PENDING_IDEMPOTENCY - OPT_IN_CREATES)
