# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Human-edited policy configuration. The model can never change it.

`PENDING_IDEMPOTENCY` keeps `tasks_create` and `reminders_create` denied in
production (`deny.tool_disabled`): creation is not yet idempotent and
`source_event_id` is not populated (P3.3 review finding M5, PR #30). A
redelivered update or a retried model call would create duplicates. Once the
M5 fix is merged, enabling them is the one-line change
`PENDING_IDEMPOTENCY = frozenset()`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from src.policy.registry import TOOL_RULES, ToolRule

PENDING_IDEMPOTENCY: frozenset[str] = frozenset({"tasks_create", "reminders_create"})


@dataclass(frozen=True)
class PolicyConfig:
    rules: Mapping[str, ToolRule] = field(default=TOOL_RULES)
    disabled_tools: frozenset[str] = PENDING_IDEMPOTENCY

    def __post_init__(self) -> None:
        if not isinstance(self.disabled_tools, frozenset):
            raise TypeError("disabled_tools must be a frozenset")
        if not isinstance(self.rules, Mapping):
            raise TypeError("rules must be a mapping")
