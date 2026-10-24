# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""src/db/crypto.py: AES-256-GCM column encryption (INV-7). Keys are
generated at runtime; nothing secret is committed. Requires the
`cryptography` package (see report: not yet in pyproject).
"""

import base64
import logging

import pytest

from src.db.crypto import KEY_ENV_VAR, CryptoError, DataCipher, generate_key


def _cipher() -> DataCipher:
    return DataCipher.from_env({KEY_ENV_VAR: generate_key()})


def test_round_trip_bytes_and_text() -> None:
    c = _cipher()
    assert c.decrypt(c.encrypt(b"\x00\x01 secret bytes")) == b"\x00\x01 secret bytes"
    assert c.decrypt_text(c.encrypt("héllo ₹")) == "héllo ₹"
    assert c.decrypt(c.encrypt(b"")) == b""


def test_ciphertext_is_versioned_randomised_and_hides_plaintext() -> None:
    c = _cipher()
    a, b = c.encrypt(b"same plaintext"), c.encrypt(b"same plaintext")
    assert a[0] == 1 and a != b  # version byte; fresh nonce each time
    assert b"same plaintext" not in a


def test_tamper_is_detected() -> None:
    c = _cipher()
    blob = bytearray(c.encrypt(b"payload"))
    for pos in (1, 13, len(blob) - 1):  # nonce, body, tag
        bad = bytearray(blob)
        bad[pos] ^= 0x01
        with pytest.raises(CryptoError):
            c.decrypt(bytes(bad))
    with pytest.raises(CryptoError):
        c.decrypt(bytes(blob[:-3]))  # truncated
    with pytest.raises(CryptoError):
        c.decrypt(b"\x02" + bytes(blob[1:]))  # unknown version


def test_wrong_key_and_wrong_context_fail() -> None:
    c1, c2 = _cipher(), _cipher()
    blob = c1.encrypt(b"x", aad=b"llm_audit:1")
    with pytest.raises(CryptoError):
        c2.decrypt(blob, aad=b"llm_audit:1")
    with pytest.raises(CryptoError):
        c1.decrypt(blob, aad=b"llm_audit:2")
    assert c1.decrypt(blob, aad=b"llm_audit:1") == b"x"


def test_key_loading_validation() -> None:
    with pytest.raises(CryptoError, match="not set"):
        DataCipher.from_env({})
    with pytest.raises(CryptoError, match="32 bytes"):
        DataCipher.from_env({KEY_ENV_VAR: base64.urlsafe_b64encode(b"short").decode()})
    with pytest.raises(CryptoError, match="base64"):
        DataCipher.from_env({KEY_ENV_VAR: "!!!not base64!!!"})


def test_inv7_key_and_plaintext_never_leak(caplog: pytest.LogCaptureFixture) -> None:
    key = generate_key()
    plaintext = "PLAINTEXT-CANARY-8841"
    caplog.set_level(logging.DEBUG)
    c = DataCipher.from_env({KEY_ENV_VAR: key})
    blob = c.encrypt(plaintext)
    surfaces = [repr(c), str(c), caplog.text]
    for bad in (blob[:-1] + b"\x00", b"short", c.encrypt(b"y")):
        try:
            DataCipher.from_env({KEY_ENV_VAR: generate_key()}).decrypt(bad)
        except CryptoError as exc:
            surfaces += [str(exc), repr(exc)]
            assert exc.__cause__ is None
    for bad_env in ({KEY_ENV_VAR: key[:-4]}, {KEY_ENV_VAR: "x" * 10}):
        try:
            DataCipher.from_env(bad_env)
        except CryptoError as exc:
            surfaces += [str(exc), repr(exc)]
    for s in surfaces:
        assert key not in s and plaintext not in s


def test_key_decoding_is_strict() -> None:
    good = generate_key()
    for bad in (good[:10] + "!!" + good[12:], good + "\n junk", " ".join([good[:20], good[20:]])):
        with pytest.raises(CryptoError):
            DataCipher.from_env({KEY_ENV_VAR: bad})
