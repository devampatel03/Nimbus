# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Canonical payload encoding and hash (ADR-0009 approval binding).

The hash binds an approval to (tool, action, payload). The encoding is
deterministic: sorted keys, no whitespace, UTF-8, no NaN/Infinity. Only JSON
types are accepted (str, int, bool, None, finite float, list, dict with str
keys); anything else raises `PayloadError`, so a payload that cannot be stored
faithfully in JSONB is refused before an approval exists (fail closed).

Pure: no I/O, no clock, no randomness.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

HASH_VERSION = "nimbus.approval.payload.v1"
MAX_DEPTH = 8
MAX_CANONICAL_BYTES = 3000  # also bounds the card (Telegram caps a message at 4096)


class PayloadError(ValueError):
    """The payload is not plain JSON, too deep or too large. Fixed message."""


def _check(value: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise PayloadError("payload too deep")
    if value is None or isinstance(value, bool | str):
        if isinstance(value, str) and "\x00" in value:
            raise PayloadError("payload contains NUL")  # JSONB cannot store it
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PayloadError("payload number not finite")
        return value
    if isinstance(value, list | tuple):
        return [_check(v, depth + 1) for v in value]
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str) or "\x00" in k:
                raise PayloadError("payload key not a string")
            out[k] = _check(v, depth + 1)
        return out
    raise PayloadError("payload value not JSON")


def plain_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A deep, plain-dict copy of `payload` (validated)."""
    if not isinstance(payload, Mapping):
        raise PayloadError("payload not an object")
    checked = _check(payload, 0)
    assert isinstance(checked, dict)
    return checked


def canonical_bytes(tool: str, action: str, payload: Mapping[str, Any]) -> bytes:
    if not isinstance(tool, str) or not isinstance(action, str) or not tool or not action:
        raise PayloadError("tool and action must be non-empty strings")
    doc = {"v": HASH_VERSION, "tool": tool, "action": action, "payload": plain_payload(payload)}
    raw = json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(raw) > MAX_CANONICAL_BYTES:
        raise PayloadError("payload too large")
    return raw


def payload_sha256(tool: str, action: str, payload: Mapping[str, Any]) -> str:
    """Lowercase hex sha256 of the canonical encoding (matches the
    `ck_pending_action_sha256` CHECK)."""
    return hashlib.sha256(canonical_bytes(tool, action, payload)).hexdigest()
