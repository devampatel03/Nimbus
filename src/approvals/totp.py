# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""TOTP step-up (RFC 6238: HMAC-SHA1, 6 digits, 30 s step, +-1 step).

Implemented on the standard library (ADR-0009 names `pyotp`; this is the same
algorithm without a new dependency, checked against the RFC 6238 appendix B
vectors in the tests). Every candidate in the window is compared with
`hmac.compare_digest`, and the loop never exits early, so timing does not say
which step matched. A code is accepted only for a step strictly after the last
accepted one (`after_step`): a code cannot be replayed within its window.

Pure: the caller supplies the time and the secret bytes.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import struct
from hashlib import sha1

STEP_S = 30
DIGITS = 6
WINDOW = 1
MIN_SECRET_BYTES = 16  # 128 bits (RFC 4226 recommends >= 160; 32 base32 chars = 160)


class TotpSecretError(ValueError):
    """The configured secret is not usable base32. Fixed message."""


def decode_secret(b32: str) -> bytes:
    cleaned = b32.replace(" ", "").upper()
    pad = "=" * (-len(cleaned) % 8)
    try:
        raw = base64.b32decode(cleaned + pad, casefold=False)
    except (binascii.Error, ValueError):
        raise TotpSecretError("totp secret is not base32") from None
    if len(raw) < MIN_SECRET_BYTES:
        raise TotpSecretError("totp secret too short")
    return raw


def code_at(secret: bytes, step: int) -> str:
    mac = hmac.new(secret, struct.pack(">Q", step), sha1).digest()
    off = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[off : off + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def step_of(epoch_s: float) -> int:
    return int(epoch_s // STEP_S)


def verify(secret: bytes, code: object, *, epoch_s: float, after_step: int | None) -> int | None:
    """The matched step, or None. `code` must be exactly six ASCII digits."""
    if not isinstance(code, str) or len(code) != DIGITS or not all(c in "0123456789" for c in code):
        return None
    now = step_of(epoch_s)
    matched: int | None = None
    presented = code.encode("ascii")
    for step in range(now - WINDOW, now + WINDOW + 1):
        ok = hmac.compare_digest(code_at(secret, step).encode("ascii"), presented)
        if ok and matched is None and (after_step is None or step > after_step):
            matched = step
    return matched
