# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Column-level encryption helper (P1.2c, INV-7).

AES-256-GCM via the `cryptography` package. The key comes from the
`NIMBUS_DATA_KEY` environment variable: 32 random bytes, base64 (standard or
URL-safe) encoded, e.g. `python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.

Ciphertext format (versioned, so the scheme can change without ambiguity):

    byte 0        format version (0x01 = AES-256-GCM, 96-bit random nonce)
    bytes 1..12   nonce
    bytes 13..    ciphertext || 16-byte GCM tag

`aad` (associated data) binds a ciphertext to its context (e.g. the column
and row id); decrypting under a different `aad` fails.

INV-7: this module never logs and never puts the key or plaintext into an
exception message, `repr()` or `str()`. All failures raise `CryptoError`
with a fixed, content-free message.
"""

import base64
import binascii
import os
from collections.abc import Mapping
from typing import Any

KEY_ENV_VAR = "NIMBUS_DATA_KEY"
FORMAT_V1 = 1
_NONCE_LEN = 12
_TAG_LEN = 16
_KEY_LEN = 32


class CryptoError(Exception):
    """Content-free error: never includes key material or plaintext."""


def _aesgcm_cls() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import (
            AESGCM,
        )
    except ImportError as exc:  # pragma: no cover - environment problem
        raise CryptoError("the 'cryptography' package is required for column encryption") from exc
    return AESGCM


def _decode_key(raw: str) -> bytes:
    padded = raw.strip()
    padded += "=" * (-len(padded) % 4)
    try:
        key = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        raise CryptoError(f"{KEY_ENV_VAR} is not valid base64") from None
    if len(key) != _KEY_LEN:
        raise CryptoError(f"{KEY_ENV_VAR} must decode to exactly {_KEY_LEN} bytes")
    return key


class DataCipher:
    """AES-256-GCM column cipher. Construct via `from_env()` or `from_key()`."""

    __slots__ = ("_aead",)

    _aead: Any

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_LEN:
            raise CryptoError(f"key must be exactly {_KEY_LEN} bytes")
        self._aead = _aesgcm_cls()(key)

    @classmethod
    def from_key(cls, key: bytes) -> "DataCipher":
        return cls(key)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DataCipher":
        env = os.environ if environ is None else environ
        raw = env.get(KEY_ENV_VAR)
        if not raw:
            raise CryptoError(f"{KEY_ENV_VAR} is not set")
        return cls(_decode_key(raw))

    def encrypt(self, plaintext: bytes | str, *, aad: bytes = b"") -> bytes:
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        nonce = os.urandom(_NONCE_LEN)
        sealed: bytes = self._aead.encrypt(nonce, data, aad or None)
        return bytes([FORMAT_V1]) + nonce + sealed

    def decrypt(self, blob: bytes, *, aad: bytes = b"") -> bytes:
        if len(blob) < 1 + _NONCE_LEN + _TAG_LEN:
            raise CryptoError("ciphertext is malformed")
        if blob[0] != FORMAT_V1:
            raise CryptoError("unsupported ciphertext version")
        nonce = blob[1 : 1 + _NONCE_LEN]
        try:
            plain: bytes = self._aead.decrypt(nonce, blob[1 + _NONCE_LEN :], aad or None)
            return plain
        except Exception:  # noqa: BLE001 - InvalidTag etc.; never chain (may hold nothing, but be safe)
            raise CryptoError(
                "decryption failed (wrong key, tampered data or wrong context)"
            ) from None

    def decrypt_text(self, blob: bytes, *, aad: bytes = b"") -> str:
        return self.decrypt(blob, aad=aad).decode("utf-8")

    def __repr__(self) -> str:
        return "DataCipher(<redacted>)"

    __str__ = __repr__


def generate_key() -> str:
    """Fresh random key in the NIMBUS_DATA_KEY format (for setup and tests)."""
    return base64.urlsafe_b64encode(os.urandom(_KEY_LEN)).decode("ascii")
