# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Policy tool registry: risk class, scope and rate limit per tool (ADR-0009).

This table is human-edited code. A tool that is not listed here is denied
(`deny.unknown_tool`), even if `src.tools` discovers it, and a tool whose
code-declared risk differs from this table is denied (`deny.risk_mismatch`),
so adding a tool needs a deliberate policy entry.

There is deliberately no generic web-fetch, browse or HTTP tool, and no mail
send tool (R6); `tests/unit/policy/test_policy_registry.py` asserts both.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from src.policy.types import Risk

# Per tool, per hour (ADR-0009 row 4). Sized for one human user: generous for
# reads, tight for writes and deletions so a looping or hijacked model is
# bounded even where a write is allowed without a tap.
READ_PER_HOUR = 120
WRITE_PER_HOUR = 30
DELETE_PER_HOUR = 10


@dataclass(frozen=True)
class ToolRule:
    risk: Risk
    scope: str
    max_calls_per_hour: int
    # The tool deletes or irreversibly changes data: approval + TOTP step-up.
    destructive: bool = False
    # The tool talks to a third-party service (denied even for READ when KILLED).
    external_service: bool = False


def _rule(
    risk: Risk, scope: str, limit: int, *, destructive: bool = False, external: bool = False
) -> ToolRule:
    return ToolRule(risk, scope, limit, destructive=destructive, external_service=external)


_R, _W = Risk.READ, Risk.WRITE_INTERNAL
_WX, _D = Risk.WRITE_EXTERNAL, Risk.DESTRUCTIVE

TOOL_RULES: Mapping[str, ToolRule] = MappingProxyType(
    {
        # Tasks (local, src/tools/integrations/tasks.py)
        "tasks_create": _rule(_W, "tasks", WRITE_PER_HOUR),
        "tasks_update": _rule(_W, "tasks", WRITE_PER_HOUR),
        "tasks_complete": _rule(_W, "tasks", WRITE_PER_HOUR),
        # A hard delete is not undoable: treated as destructive although the
        # tool's own risk class is WRITE_INTERNAL.
        "tasks_delete": _rule(_W, "tasks", DELETE_PER_HOUR, destructive=True),
        "tasks_get": _rule(_R, "tasks", READ_PER_HOUR),
        "tasks_list": _rule(_R, "tasks", READ_PER_HOUR),
        # Reminders (local, src/tools/integrations/reminders.py)
        "reminders_create": _rule(_W, "reminders", WRITE_PER_HOUR),
        "reminders_update": _rule(_W, "reminders", WRITE_PER_HOUR),
        "reminders_delete": _rule(_W, "reminders", DELETE_PER_HOUR, destructive=True),
        "reminders_get": _rule(_R, "reminders", READ_PER_HOUR),
        "reminders_list": _rule(_R, "reminders", READ_PER_HOUR),
        # Google Drive, read only (src/tools/integrations/drive_read.py, P3.8).
        # Scope "drive" is not local: enabled only from the OAuth token store.
        "drive_search": _rule(_R, "drive", READ_PER_HOUR, external=True),
        "drive_read": _rule(_R, "drive", READ_PER_HOUR, external=True),
        # Google Calendar (src/tools/integrations/google_calendar.py, P3.5).
        # Scope "calendar" is NOT in LOCAL_SCOPES: denied until the app enables it.
        "calendar_list": _rule(_R, "calendar", READ_PER_HOUR, external=True),
        "calendar_get": _rule(_R, "calendar", READ_PER_HOUR, external=True),
        "calendar_create": _rule(_WX, "calendar", WRITE_PER_HOUR, external=True),
        "calendar_update": _rule(_WX, "calendar", WRITE_PER_HOUR, external=True),
        "calendar_delete": _rule(_D, "calendar", DELETE_PER_HOUR, destructive=True, external=True),
    }
)

# Scopes that need no grant (purely local tools). Google scopes are added by
# the caller from the OAuth token store once those tools exist (P3.5+).
LOCAL_SCOPES: frozenset[str] = frozenset({"tasks", "reminders"})
