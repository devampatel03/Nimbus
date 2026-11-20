# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Canary tripwires (P4.4, ADR-0023): planted values (`tokens`), an
obfuscation-tolerant detector (`detect`), the outbound guard (`guard`), the
Telegram decorator (`gateway`) and the database sweep (`sweep`).

Deliberately import-light: the orchestrator and `src.net` import the pure
modules, so this package does not import `gateway` or `sweep` here."""

from src.canary.detect import CanaryDetector, CanaryHit, skeleton
from src.canary.guard import (
    ALERT_TEXT,
    BLOCKED_REPLY,
    DENIED_TOOL,
    CanaryBlocked,
    CanaryGuard,
)
from src.canary.settings import CanarySettings
from src.canary.tokens import CanarySet, Site, derive_canaries, generate_canaries

__all__ = [
    "ALERT_TEXT",
    "BLOCKED_REPLY",
    "DENIED_TOOL",
    "CanaryBlocked",
    "CanaryDetector",
    "CanaryGuard",
    "CanaryHit",
    "CanarySet",
    "CanarySettings",
    "Site",
    "derive_canaries",
    "generate_canaries",
    "skeleton",
]
