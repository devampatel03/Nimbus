# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Encrypted token store (INV-7, R12): the refresh token is AES-256-GCM
ciphertext (src/db/crypto.py) in `system_flag`; plaintext never touches the
table; wrong key / wrong context / tampering fail with a fixed code. Uses the
real test Postgres (per-session database); no new table is required."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from src.db.crypto import DataCipher, generate_key
from src.integrations.google.auth import (
    BASE_SCOPES,
    CREDENTIALS_AAD,
    CREDENTIALS_FLAG_KEY,
    GoogleAuthError,
    GoogleReauthRequired,
    PostgresTokenStore,
    StoredCredentials,
)
from src.memory.schemas.ops import SystemFlag
from tests.unit.integrations.google.auth_helpers import (
    REFRESH_TOKEN,
    SCOPE_CAL,
    SCOPE_GCOMPOSE,
    SCOPE_SEND,
)

NOW = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)


def _creds(scopes: frozenset[str] = BASE_SCOPES) -> StoredCredentials:
    return StoredCredentials(refresh_token=SecretStr(REFRESH_TOKEN), scopes=scopes, obtained_at=NOW)


def _cipher() -> DataCipher:
    return DataCipher.from_key(base64.urlsafe_b64decode(generate_key()))


@pytest.fixture
async def sf(pg_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    conn: AsyncConnection
    async with pg_engine.connect() as conn:
        tx = await conn.begin()
        yield async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        await tx.rollback()


async def test_roundtrip_and_ciphertext_at_rest(sf: async_sessionmaker[AsyncSession]) -> None:
    store = PostgresTokenStore(sf, _cipher())
    assert await store.load() is None
    await store.save(_creds())
    loaded = await store.load()
    assert loaded is not None
    assert loaded.refresh_token.get_secret_value() == REFRESH_TOKEN
    assert loaded.scopes == BASE_SCOPES
    assert loaded.obtained_at == NOW

    async with sf() as s:
        raw = (
            await s.execute(
                text("SELECT value::text FROM system_flag WHERE key = :k"),
                {"k": CREDENTIALS_FLAG_KEY},
            )
        ).scalar_one()
        everything = (
            await s.execute(
                text("SELECT coalesce(string_agg(t::text, ' '), '') FROM system_flag t")
            )
        ).scalar_one()
    assert REFRESH_TOKEN not in raw and REFRESH_TOKEN not in everything
    value = json.loads(raw)
    assert value["v"] == 1
    # Only the ciphertext field may hold token material, and only encrypted:
    # the plaintext prefix must not sit anywhere else in the row (the random
    # base64 ciphertext itself can contain "1//", so it is excluded).
    assert "1//" not in raw.replace(value["ct"], "")
    blob = base64.b64decode(value["ct"])
    assert REFRESH_TOKEN.encode() not in blob
    assert blob[0] == 1 and len(blob) > 40  # versioned AES-GCM blob from src/db/crypto.py


async def test_save_overwrites_and_delete_removes(sf: async_sessionmaker[AsyncSession]) -> None:
    store = PostgresTokenStore(sf, _cipher())
    await store.save(_creds())
    await store.save(_creds(frozenset({SCOPE_CAL})))
    loaded = await store.load()
    assert loaded is not None and loaded.scopes == frozenset({SCOPE_CAL})
    await store.delete()
    assert await store.load() is None
    await store.delete()  # idempotent


async def test_wrong_key_fails_with_fixed_code_and_no_leak(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    await PostgresTokenStore(sf, _cipher()).save(_creds())
    with pytest.raises(GoogleAuthError) as exc:
        await PostgresTokenStore(sf, _cipher()).load()
    assert exc.value.code == "token_decrypt_failed"
    assert not isinstance(exc.value, GoogleReauthRequired) or exc.value.needs_reauth
    assert REFRESH_TOKEN not in str(exc.value)


async def test_ciphertext_bound_to_its_context_via_aad(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A blob sealed for another context (AAD) must not decrypt here."""
    cipher = _cipher()
    foreign = cipher.encrypt(b"{}", aad=b"some other context")
    async with sf() as s, s.begin():
        s.add(
            SystemFlag(
                key=CREDENTIALS_FLAG_KEY, value={"v": 1, "ct": base64.b64encode(foreign).decode()}
            )
        )
    with pytest.raises(GoogleAuthError) as exc:
        await PostgresTokenStore(sf, cipher).load()
    assert exc.value.code == "token_decrypt_failed"


@pytest.mark.parametrize(
    "value",
    [
        {"v": 2, "ct": "AAAA"},
        {"v": 1},
        {"v": 1, "ct": "!!!not-base64!!!"},
        {"v": 1, "ct": 7},
        {},
    ],
)
async def test_malformed_row_is_a_fixed_code_error(
    sf: async_sessionmaker[AsyncSession], value: dict[str, object]
) -> None:
    async with sf() as s, s.begin():
        s.add(SystemFlag(key=CREDENTIALS_FLAG_KEY, value=value))
    with pytest.raises(GoogleAuthError) as exc:
        await PostgresTokenStore(sf, _cipher()).load()
    assert exc.value.code == "token_store_corrupt"


async def test_store_never_saves_credentials_with_out_of_set_scopes(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    store = PostgresTokenStore(sf, _cipher())
    with pytest.raises(GoogleReauthRequired) as exc:
        await store.save(_creds(frozenset({SCOPE_CAL, SCOPE_SEND})))
    assert exc.value.code == "scope_violation"
    assert await store.load() is None


async def test_decrypted_scope_tampering_is_caught_on_load(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Even a validly-encrypted blob carrying an extra scope is rejected on load."""
    cipher = _cipher()
    payload = json.dumps(
        {
            "refresh_token": REFRESH_TOKEN,
            "scopes": [SCOPE_CAL, SCOPE_SEND],
            "obtained_at": NOW.isoformat(),
        }
    )
    blob = cipher.encrypt(payload, aad=CREDENTIALS_AAD)
    async with sf() as s, s.begin():
        s.add(
            SystemFlag(
                key=CREDENTIALS_FLAG_KEY, value={"v": 1, "ct": base64.b64encode(blob).decode()}
            )
        )
    with pytest.raises(GoogleReauthRequired) as exc:
        await PostgresTokenStore(sf, cipher).load()
    assert exc.value.code == "scope_violation"


async def test_compose_scope_is_rejected_on_save_and_load_unless_flag_on(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    cipher = _cipher()
    with_compose = _creds(frozenset({SCOPE_CAL, SCOPE_GCOMPOSE}))
    off = PostgresTokenStore(sf, cipher)
    with pytest.raises(GoogleReauthRequired) as exc:
        await off.save(with_compose)
    assert exc.value.code == "scope_violation"
    assert await off.load() is None

    on = PostgresTokenStore(sf, cipher, allow_compose=True)
    await on.save(with_compose)
    loaded = await on.load()
    assert loaded is not None and SCOPE_GCOMPOSE in loaded.scopes

    # the flag was switched off after the token was stored: store.load refuses it
    with pytest.raises(GoogleReauthRequired) as exc2:
        await off.load()
    assert exc2.value.code == "scope_violation"
