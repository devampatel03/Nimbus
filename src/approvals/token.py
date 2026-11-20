# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""The approval token carried in Telegram `callback_data` (ADR-0009).

    callback_data = verb ":" base64url(action_id[16] || mac[16])      (46 bytes)
    mac = HMAC-SHA256(K, DOMAIN || verb || action_id || sha256(payload)
                          || nonce || expiry_epoch_s || user_id)[:16]

`verb` is `ap` (approve) or `rj` (reject), so an approve button cannot be
re-labelled as the other one. The payload, its hash, the nonce and the expiry
live server-side in `pending_action`; the token only names the row and proves
that this process minted it for this user, this exact payload hash, nonce and
expiry. Verification recomputes the MAC from the stored row and compares in
constant time (`hmac.compare_digest`). A 128-bit tag is ample for an online
forgery (each guess is one Telegram callback from the one allowlisted user).

Pure: no I/O, no clock. The key is passed in as bytes and never logged.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import uuid
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

DOMAIN = b"nimbus/approval-token/v1\x00"
MAC_BYTES = 16
MAX_CALLBACK_BYTES = 64  # Telegram Bot API limit for callback_data
MIN_KEY_BYTES = 32


class Verb(StrEnum):
    APPROVE = "ap"
    REJECT = "rj"


class TokenError(ValueError):
    """Malformed token. Fixed message; never echoes input."""


@dataclass(frozen=True)
class ParsedToken:
    verb: Verb
    action_id: uuid.UUID
    mac: bytes


def _u64(n: int, what: str) -> bytes:
    if type(n) is not int or n < 0 or n >= 1 << 64:
        raise TokenError(f"{what} out of range")
    return n.to_bytes(8, "big")


def compute_mac(
    key: bytes,
    *,
    verb: Verb,
    action_id: uuid.UUID,
    payload_sha256_hex: str,
    nonce: bytes,
    expiry_epoch_s: int,
    user_id: int,
) -> bytes:
    if not isinstance(key, bytes) or len(key) < MIN_KEY_BYTES:
        raise TokenError("key too short")
    if not isinstance(nonce, bytes) or len(nonce) < 16:
        raise TokenError("nonce too short")
    try:
        digest = bytes.fromhex(payload_sha256_hex)
    except (ValueError, TypeError):
        raise TokenError("bad payload hash") from None
    if len(digest) != 32:
        raise TokenError("bad payload hash")
    msg = b"".join(
        (
            DOMAIN,
            Verb(verb).value.encode("ascii"),
            action_id.bytes,
            digest,
            len(nonce).to_bytes(1, "big"),
            nonce,
            _u64(expiry_epoch_s, "expiry"),
            _u64(user_id, "user id"),
        )
    )
    return hmac.new(key, msg, sha256).digest()[:MAC_BYTES]


def encode(verb: Verb, action_id: uuid.UUID, mac: bytes) -> str:
    if len(mac) != MAC_BYTES:
        raise TokenError("bad mac length")
    body = base64.urlsafe_b64encode(action_id.bytes + mac).rstrip(b"=").decode("ascii")
    data = f"{Verb(verb).value}:{body}"
    if len(data.encode("ascii")) > MAX_CALLBACK_BYTES:  # pragma: no cover - fixed size
        raise TokenError("token too long")
    return data


_BODY_LEN = 43  # base64url of 32 bytes, unpadded


def parse(data: object) -> ParsedToken:
    """Strict: exactly `ap:` or `rj:` + 43 url-safe base64 chars."""
    if not isinstance(data, str) or len(data) != 3 + _BODY_LEN or data[2] != ":":
        raise TokenError("malformed token")
    try:
        verb = Verb(data[:2])
    except ValueError:
        raise TokenError("malformed token") from None
    body = data[3:]
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in body):
        raise TokenError("malformed token")
    try:
        raw = base64.urlsafe_b64decode(body + "=")
    except (binascii.Error, ValueError):
        raise TokenError("malformed token") from None
    # Reject non-canonical encodings (the last char carries 2 unused bits).
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != body:
        raise TokenError("malformed token")
    return ParsedToken(verb, uuid.UUID(bytes=raw[:16]), raw[16:])


def mac_matches(expected: bytes, presented: bytes) -> bool:
    """Constant-time comparison (the only MAC comparison in the package)."""
    return hmac.compare_digest(expected, presented)
