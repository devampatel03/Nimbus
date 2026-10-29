# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.4 follow-ups (before the first live run, H4):

* `load_for_revoke`: revoke works on a stored token whose scopes are out of
  policy (the normal `load` refuses it, so it could only be forgotten locally
  and never revoked at Google).
* The token store and the token client must agree on the gmail.compose flag;
  `GoogleAuth` refuses a disagreeing pair (fail closed at construction).
* `GOOGLE_ALLOW_GMAIL_COMPOSE` is a strict bool: only `true` / `false`.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from src.db.crypto import DataCipher, generate_key
from src.integrations.google.auth import (
    BASE_SCOPES,
    GoogleAuth,
    GoogleAuthError,
    GoogleOAuthSettings,
    GoogleReauthRequired,
    GoogleTokenClient,
    InMemoryTokenStore,
    PostgresTokenStore,
    StoredCredentials,
)
from tests.unit.integrations.google.auth_helpers import (
    REFRESH_TOKEN,
    SCOPE_CAL,
    SCOPE_GCOMPOSE,
    SCOPE_SEND,
    FakeTokenEndpoint,
    Sleeps,
    auth_settings,
    json_response,
)

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def creds(scopes: frozenset[str] = BASE_SCOPES) -> StoredCredentials:
    return StoredCredentials(SecretStr(REFRESH_TOKEN), scopes, NOW)


def client(fake: FakeTokenEndpoint, *, allow_compose: bool = False) -> GoogleTokenClient:
    return GoogleTokenClient(
        auth_settings(allow_gmail_compose=allow_compose), fake.egress(), sleep=Sleeps()
    )


# ---------------------------------------------------------------- load_for_revoke


async def test_in_memory_store_load_for_revoke_skips_the_lint() -> None:
    store = InMemoryTokenStore(creds(frozenset({SCOPE_CAL, SCOPE_SEND})))
    got = await store.load_for_revoke()
    assert got is not None and got.refresh_token.get_secret_value() == REFRESH_TOKEN


async def test_out_of_policy_token_is_revoked_at_google_then_forgotten() -> None:
    fake = FakeTokenEndpoint(json_response(200, {}))
    store = InMemoryTokenStore(creds(frozenset({SCOPE_CAL, SCOPE_SEND})))
    auth = GoogleAuth(client(fake), store)
    await auth.revoke_and_forget()  # no revoke_failed: it really was revoked
    assert fake.paths == ["/revoke"] and fake.forms[0]["token"] == REFRESH_TOKEN
    assert await store.load() is None


class _NoRevokeLoader(InMemoryTokenStore):
    """A store without `load_for_revoke` whose `load` refuses (old behaviour)."""

    load_for_revoke = None  # type: ignore[assignment]

    async def load(self) -> StoredCredentials | None:
        raise GoogleReauthRequired("scope_violation")


async def test_store_without_load_for_revoke_still_forgets_and_reports() -> None:
    fake = FakeTokenEndpoint()
    store = _NoRevokeLoader(creds())
    auth = GoogleAuth(client(fake), store)
    with pytest.raises(GoogleAuthError) as ei:
        await auth.revoke_and_forget()
    assert ei.value.code == "revoke_failed" and fake.paths == []


class _BrokenRevokeLoader(InMemoryTokenStore):
    async def load_for_revoke(self) -> StoredCredentials | None:
        raise GoogleAuthError("token_decrypt_failed")


async def test_undecryptable_token_is_forgotten_and_reported() -> None:
    fake = FakeTokenEndpoint()
    store = _BrokenRevokeLoader(creds())
    with pytest.raises(GoogleAuthError) as ei:
        await GoogleAuth(client(fake), store).revoke_and_forget()
    assert ei.value.code == "revoke_failed" and fake.paths == []
    assert store._creds is None


@pytest.fixture
async def sf(pg_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    conn: AsyncConnection
    async with pg_engine.connect() as conn:
        tx = await conn.begin()
        yield async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        await tx.rollback()


async def test_postgres_load_for_revoke_reads_an_out_of_policy_record(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    cipher = DataCipher.from_key(base64.urlsafe_b64decode(generate_key()))
    # Stored while the compose flag was on; the flag is now off.
    await PostgresTokenStore(sf, cipher, allow_compose=True).save(
        creds(frozenset({SCOPE_CAL, SCOPE_GCOMPOSE}))
    )
    off = PostgresTokenStore(sf, cipher)
    with pytest.raises(GoogleReauthRequired):
        await off.load()
    got = await off.load_for_revoke()
    assert got is not None and got.refresh_token.get_secret_value() == REFRESH_TOKEN
    assert SCOPE_GCOMPOSE in got.scopes
    fake = FakeTokenEndpoint(json_response(200, {}))
    await GoogleAuth(client(fake), off).revoke_and_forget()
    assert fake.paths == ["/revoke"] and await off.load_for_revoke() is None


async def test_postgres_load_for_revoke_of_empty_store_is_none(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    cipher = DataCipher.from_key(base64.urlsafe_b64decode(generate_key()))
    assert await PostgresTokenStore(sf, cipher).load_for_revoke() is None


async def test_postgres_load_for_revoke_with_wrong_key_is_a_fixed_code(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    a = DataCipher.from_key(base64.urlsafe_b64decode(generate_key()))
    b = DataCipher.from_key(base64.urlsafe_b64decode(generate_key()))
    await PostgresTokenStore(sf, a).save(creds())
    with pytest.raises(GoogleAuthError) as ei:
        await PostgresTokenStore(sf, b).load_for_revoke()
    assert ei.value.code == "token_decrypt_failed"
    assert REFRESH_TOKEN not in str(ei.value)


# ---------------------------------------------------------------- compose flags agree


@pytest.mark.parametrize(("store_flag", "client_flag"), [(True, False), (False, True)])
def test_disagreeing_compose_flags_are_refused(store_flag: bool, client_flag: bool) -> None:
    fake = FakeTokenEndpoint()
    with pytest.raises(GoogleAuthError) as ei:
        GoogleAuth(
            client(fake, allow_compose=client_flag),
            InMemoryTokenStore(creds(), allow_compose=store_flag),
        )
    assert ei.value.code == "compose_flag_mismatch"


@pytest.mark.parametrize("flag", [True, False])
def test_agreeing_compose_flags_are_accepted(flag: bool) -> None:
    fake = FakeTokenEndpoint()
    GoogleAuth(client(fake, allow_compose=flag), InMemoryTokenStore(creds(), allow_compose=flag))


def test_stores_expose_their_compose_flag() -> None:
    assert InMemoryTokenStore(allow_compose=True).allow_compose is True
    cipher = DataCipher.from_key(base64.urlsafe_b64decode(generate_key()))
    assert PostgresTokenStore(None, cipher).allow_compose is False  # type: ignore[arg-type]


# ---------------------------------------------------------------- strict bool


@pytest.mark.parametrize(
    ("raw", "value"),
    [("true", True), ("false", False), ("TRUE", True), (" False ", False), (True, True)],
)
def test_compose_flag_accepts_only_true_or_false(raw: Any, value: bool) -> None:
    assert GoogleOAuthSettings(_env_file=None, allow_gmail_compose=raw).allow_gmail_compose is value


@pytest.mark.parametrize("raw", ["1", "yes", "on", "y", "t", "0", "no", "off", "", "tru", 1, 0])
def test_lax_truthy_values_are_refused(raw: Any) -> None:
    with pytest.raises(ValidationError) as ei:
        GoogleOAuthSettings(_env_file=None, allow_gmail_compose=raw)
    assert "allow_gmail_compose" in str(ei.value)


@pytest.mark.parametrize("raw", ["1", "yes", "on"])
def test_lax_env_value_is_refused(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_ALLOW_GMAIL_COMPOSE", raw)
    with pytest.raises(ValidationError):
        GoogleOAuthSettings(_env_file=None)
