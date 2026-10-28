# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Approval card text (pure). The card shows the TRUE payload, built by code
from the stored row: tool, action, every argument, the absolute expiry time and
whether a step-up code will be asked. No model prose is ever included.

Arguments are model-chosen, so invisible or direction-changing characters
(bidi overrides, zero-width, other format/control/private-use code points,
unusual spaces) are shown as `\\uXXXX` escapes: what the user reads is what
runs. Look-alike letters (Cyrillic "a" for Latin "a") are NOT normalised:
residual risk, the card cannot prove intent.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

_VISIBLE_EXCEPTIONS = {"\n", " "}
_HIDDEN_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp", "Zs"}


def reveal(text: str) -> str:
    """Escape every character a reader could not see or that reorders text."""
    out: list[str] = []
    for ch in text:
        if ch not in _VISIBLE_EXCEPTIONS and unicodedata.category(ch) in _HIDDEN_CATEGORIES:
            cp = ord(ch)
            out.append(f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}")
        else:
            out.append(ch)
    return "".join(out)


def tz_label(offset_minutes: int) -> str:
    if offset_minutes == 330:
        return "IST"
    sign = "+" if offset_minutes >= 0 else "-"
    h, m = divmod(abs(offset_minutes), 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def absolute_time(at: datetime, offset_minutes: int) -> str:
    local = at.astimezone(timezone(timedelta(minutes=offset_minutes)))
    return f"{local.strftime('%d %b %Y, %H:%M:%S')} {tz_label(offset_minutes)}"


def render_card(
    *,
    tool: str,
    action: str,
    payload: Mapping[str, Any],
    expires_at: datetime,
    now: datetime,
    offset_minutes: int,
    step_up: bool,
    rule_id: str,
) -> str:
    args = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    minutes = max(0, int((expires_at - now).total_seconds() // 60))
    lines = [
        "Approve this exact action?",
        f"Tool: {reveal(tool)}",
        f"Action: {reveal(action)}",
        "Arguments (exactly what will run):",
        reveal(args),
        f"Expires: {absolute_time(expires_at, offset_minutes)} (in about {minutes} min)",
        (
            "Step-up: after Approve you must enter your authenticator code."
            if step_up
            else "Step-up: not required."
        ),
        f"Policy rule: {reveal(rule_id)}",
        "Only the arguments above are approved. If anything looks wrong, tap Reject.",
    ]
    return "\n".join(lines)


def render_step_up_prompt(
    *, tool: str, entered: int, expires_at: datetime, offset_minutes: int
) -> str:
    return (
        f"Step-up for {reveal(tool)}: enter the 6-digit code from your authenticator app "
        f"using the keypad. Entered: {'*' * entered}{'_' * (6 - entered)}. "
        f"Code entry closes at {absolute_time(expires_at, offset_minutes)}."
    )
