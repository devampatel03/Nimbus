# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Google OAuth for Nimbus: one-time loopback consent flow, encrypted refresh
token store, and access-token refresh (P3.4; ADR-0005, ADR-0007, ADR-0004,
ADR-0019; R3, R6, R12, R13, R15; INV-7).

How it fits together
--------------------
* ``scripts/google_oauth.py authorize`` runs on the OPERATOR'S machine (never in
  the bot process, so the app opens no inbound port, R3). The consent flow
  (PKCE, one-shot ``127.0.0.1`` listener, state check, code exchange) lives in
  ``consent.py``, which only the script imports.
* The refresh token is stored ONLY as AES-256-GCM ciphertext (``src/db/crypto``)
  inside the existing ``system_flag`` table (key ``google_oauth_credentials``),
  with an AAD that binds it to that row. No new table or migration.
* The bot process uses :class:`GoogleAuth` to turn the stored refresh token into
  short-lived access tokens (kept in memory only). Every request to Google goes
  through ``src.net`` (``Service.GOOGLE_OAUTH``: ``POST /token`` and
  ``POST /revoke`` on ``oauth2.googleapis.com`` only). This module imports no
  HTTP transport itself (INV-1). The browser, not this process, talks to
  ``accounts.google.com``.

Scopes (exact, enforced)
------------------------
Base set (``BASE_SCOPES``): ``calendar.events``, ``gmail.readonly``,
``drive.readonly`` (H17). ``gmail.compose`` (drafts) is NOT in the base set:
whether to ever request it is the human decision H16 (ADR-0007, Proposed), so
it is allowed only when ``GOOGLE_ALLOW_GMAIL_COMPOSE`` is set
(``allowed_scopes(allow_compose=True)``). That EFFECTIVE set is what scope-lint
uses at every point: the consent URL, the code-exchange grant, every refresh
response and every read from the store. Anything else -- the Gmail send and
modify scopes, the full-mailbox scope, full Drive, full Calendar -- is never
allowed. A grant that reports a scope outside the effective set (or, at
refresh, any scope the stored token did not already have) is never used
(revoked best-effort at consent time). Granting a SUBSET is fine (granular
consent); callers pass ``required_scopes`` to :meth:`GoogleAuth.access_token`.

R15 -- publishing status (operator action, H4)
----------------------------------------------
The Google Cloud OAuth consent screen MUST be set to publishing status
"In production". An app left in "Testing" gets refresh tokens that Google
expires after 7 days, which silently kills calendar/Gmail/Drive access one
week after setup. An unverified "In production" app shows an "unverified app"
warning on the consent screen (one manual click-through: Advanced -> Go to
<app> (unsafe)) and is capped at 100 users, which is fine for a personal
assistant. See ``docs/runbooks/google_oauth.md``.

Failure model (INV-7)
---------------------
Every failure is a :class:`GoogleAuthError` carrying a fixed ``code``; its
message is only ``google auth error: <code>``. No token, authorization code,
PKCE verifier, client secret, URL, request body or upstream error text is ever
put in an exception, log line or notification. Secrets are ``SecretStr``.
``invalid_grant`` / ``no_token`` / ``scope_violation`` / ``scope_missing``
raise :class:`GoogleReauthRequired` and call the optional notifier ONCE (re-armed
after ``renotify_after_s``) with a fixed message, which the app wires to a
Telegram message. A dead refresh token is remembered (by hash) so later calls
fail fast instead of hammering Google. Transient failures (network, 5xx, 429)
retry with capped exponential backoff, at most ``max_retries`` times.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.crypto import CryptoError, DataCipher
from src.memory.schemas.ops import SystemFlag
from src.net import EgressClient, EgressDenied, EgressError, EgressResponseTooLarge, Service

logger = logging.getLogger("nimbus.integrations.google.auth")

# --------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------

SCOPE_CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"  # create/edit events only
SCOPE_GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"  # search/read (untrusted)
SCOPE_GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"  # drafts (ADR-0007, H16)
SCOPE_DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"  # read-only Drive (H17)

# Allowed with no opt-in.
BASE_SCOPES: frozenset[str] = frozenset(
    {SCOPE_CALENDAR_EVENTS, SCOPE_GMAIL_READONLY, SCOPE_DRIVE_READONLY}
)
# Absolute ceiling: base + the H16-gated draft scope. Nothing outside this is
# ever acceptable, whatever the flags.
ALLOWED_SCOPES: frozenset[str] = BASE_SCOPES | {SCOPE_GMAIL_COMPOSE}
DEFAULT_SCOPES: tuple[str, ...] = tuple(sorted(BASE_SCOPES))


def allowed_scopes(*, allow_compose: bool) -> frozenset[str]:
    """The effective allowed set: base, plus gmail.compose iff opted in (H16)."""
    return BASE_SCOPES | {SCOPE_GMAIL_COMPOSE} if allow_compose else frozenset(BASE_SCOPES)


class GoogleAuthError(Exception):
    """Fixed-code failure. The message never contains anything but the code."""

    needs_reauth = False

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"google auth error: {code}")


class GoogleReauthRequired(GoogleAuthError):
    """The operator must run `scripts/google_oauth.py authorize` again."""

    needs_reauth = True


def parse_scope_string(scope: str) -> frozenset[str]:
    return frozenset(scope.split())


def lint_scopes(scopes: Iterable[str], *, allow_compose: bool = False) -> frozenset[str]:
    """Return `scopes` as a set if it is a non-empty subset of the effective
    allowed set; otherwise raise `GoogleReauthRequired("scope_violation")`. The
    offending scope is deliberately not put in the error."""
    granted = frozenset(scopes)
    if not granted or not granted <= allowed_scopes(allow_compose=allow_compose):
        raise GoogleReauthRequired("scope_violation")
    return granted


# --------------------------------------------------------------------------
# Settings (module-local, SETTINGS_PATTERN.md)
# --------------------------------------------------------------------------


class GoogleOAuthSettings(BaseSettings):
    """Env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET (+ optional tuning knobs)."""

    model_config = SettingsConfigDict(
        env_prefix="GOOGLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    # H16 (ADR-0007) is a pending human decision: gmail.compose is OFF unless
    # the operator explicitly opts in. Never default this to True in code.
    allow_gmail_compose: bool = False
    max_retries: int = Field(default=2, ge=0, le=5)
    backoff_base_s: float = Field(default=0.5, ge=0, le=30)
    backoff_max_s: float = Field(default=8.0, ge=0, le=120)
    request_timeout_s: float = Field(default=15.0, gt=0, le=60)

    @field_validator("allow_gmail_compose", mode="before")
    @classmethod
    def _strict_bool(cls, value: object) -> bool:
        """Only `true` / `false` (any case). Lax truthy strings ("1", "yes",
        "on") are refused: this flag widens the scope ceiling (H16)."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ValueError("must be true or false")

    def require_client(self) -> tuple[SecretStr, SecretStr]:
        if self.client_id is None or self.client_secret is None:
            raise GoogleAuthError("client_not_configured")
        if not self.client_id.get_secret_value() or not self.client_secret.get_secret_value():
            raise GoogleAuthError("client_not_configured")
        return self.client_id, self.client_secret


# --------------------------------------------------------------------------
# Token endpoint client (through src.net only)
# --------------------------------------------------------------------------

_FORM_HEADERS = {"content-type": "application/x-www-form-urlencoded", "accept": "application/json"}
_MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
_MAX_EXPIRES_IN_S = 24 * 3600
_OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_client",
        "unauthorized_client",
        "invalid_request",
        "invalid_scope",
        "unsupported_grant_type",
    }
)


@dataclass(frozen=True)
class TokenGrant:
    """A parsed token-endpoint success. `scopes` is NOT yet linted: callers
    must run `lint_scopes` before using or persisting anything."""

    access_token: SecretStr
    expires_in: int
    scopes: frozenset[str] | None
    refresh_token: SecretStr | None


def _json_dict(content: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(content)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class GoogleTokenClient:
    def __init__(
        self,
        settings: GoogleOAuthSettings,
        egress: EgressClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._egress = egress
        self._sleep = sleep
        self._jitter = jitter

    @property
    def allow_compose(self) -> bool:
        return self._settings.allow_gmail_compose

    async def exchange_code(
        self, code: SecretStr, verifier: SecretStr, redirect_uri: str
    ) -> TokenGrant:
        client_id, client_secret = self._settings.require_client()
        resp = await self._post(
            "/token",
            {
                "grant_type": "authorization_code",
                "code": code.get_secret_value(),
                "code_verifier": verifier.get_secret_value(),
                "redirect_uri": redirect_uri,
                "client_id": client_id.get_secret_value(),
                "client_secret": client_secret.get_secret_value(),
            },
        )
        return self._grant(resp.status_code, resp.content)

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        client_id, client_secret = self._settings.require_client()
        resp = await self._post(
            "/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token.get_secret_value(),
                "client_id": client_id.get_secret_value(),
                "client_secret": client_secret.get_secret_value(),
            },
        )
        return self._grant(resp.status_code, resp.content)

    async def revoke(self, token: SecretStr) -> None:
        """Revoke a refresh (or access) token. Already-revoked counts as success."""
        try:
            resp = await self._post("/revoke", {"token": token.get_secret_value()})
        except GoogleAuthError:
            raise GoogleAuthError("revoke_failed") from None
        if 200 <= resp.status_code < 300:
            return
        body = _json_dict(resp.content)
        if resp.status_code == 400 and body is not None and body.get("error") == "invalid_token":
            return
        raise GoogleAuthError("revoke_failed")

    def _delay(self, attempt: int) -> float:
        s = self._settings
        ceiling = min(s.backoff_max_s, s.backoff_base_s * (2**attempt))
        return float(ceiling / 2 + self._jitter() * ceiling / 2)  # "equal jitter"

    async def _post(self, path: str, form: dict[str, str]) -> Any:
        body = urlencode(form).encode("ascii")
        attempts = self._settings.max_retries + 1
        failure = "network_error"
        for attempt in range(attempts):
            try:
                resp = await self._egress.request(
                    Service.GOOGLE_OAUTH,
                    "POST",
                    path,
                    content=body,
                    headers=_FORM_HEADERS,
                    timeout_s=self._settings.request_timeout_s,
                    max_bytes=_MAX_TOKEN_RESPONSE_BYTES,
                )
            except EgressDenied:
                raise GoogleAuthError("egress_denied") from None
            except EgressResponseTooLarge:
                raise GoogleAuthError("bad_response") from None
            except EgressError:
                failure = "network_error"
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    failure = "server_error"
                else:
                    return resp
            if attempt + 1 < attempts:
                await self._sleep(self._delay(attempt))
        logger.warning("google token endpoint gave up code=%s attempts=%d", failure, attempts)
        raise GoogleAuthError(failure)

    @staticmethod
    def _grant(status: int, content: bytes) -> TokenGrant:
        data = _json_dict(content)
        if not 200 <= status < 300:
            error = data.get("error") if data is not None else None
            if error == "invalid_grant":
                raise GoogleReauthRequired("invalid_grant")
            if isinstance(error, str) and error in _OAUTH_ERROR_CODES:
                raise GoogleAuthError(error)
            raise GoogleAuthError("token_endpoint_rejected")
        if data is None:
            raise GoogleAuthError("bad_response")
        access = data.get("access_token")
        expires = data.get("expires_in")
        scope = data.get("scope")
        refresh = data.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise GoogleAuthError("bad_response")
        if (
            isinstance(expires, bool)
            or not isinstance(expires, int)
            or not 0 < expires <= _MAX_EXPIRES_IN_S
        ):
            raise GoogleAuthError("bad_response")
        if scope is not None and not isinstance(scope, str):
            raise GoogleAuthError("bad_response")
        if refresh is not None and (not isinstance(refresh, str) or not refresh):
            raise GoogleAuthError("bad_response")
        return TokenGrant(
            access_token=SecretStr(access),
            expires_in=expires,
            scopes=parse_scope_string(scope) if scope is not None else None,
            refresh_token=SecretStr(refresh) if refresh is not None else None,
        )


# --------------------------------------------------------------------------
# Token store
# --------------------------------------------------------------------------

CREDENTIALS_FLAG_KEY = "google_oauth_credentials"
CREDENTIALS_AAD = b"system_flag:google_oauth_credentials:v1"
_STORE_VERSION = 1


@dataclass(frozen=True)
class StoredCredentials:
    refresh_token: SecretStr
    scopes: frozenset[str]
    obtained_at: datetime


class TokenStore(Protocol):
    # The gmail.compose flag this store lints with. `GoogleAuth` refuses a store
    # whose flag is missing or differs from the token client's (P3.4).
    @property
    def allow_compose(self) -> bool: ...

    async def load(self) -> StoredCredentials | None: ...
    async def save(self, creds: StoredCredentials) -> None: ...
    async def delete(self) -> None: ...


# Optional on a `TokenStore` (both stores here have it): read the record WITHOUT
# scope-lint, for `revoke_and_forget` only, so an out-of-policy token can still
# be revoked at Google. Never use it to hand out access.
RevokeLoader = Callable[[], Awaitable[StoredCredentials | None]]


class InMemoryTokenStore:
    """Test / dry-run store. `initial` is deliberately not linted so tests can
    seed a hostile record; `save` lints like the real store."""

    def __init__(
        self, initial: StoredCredentials | None = None, *, allow_compose: bool = False
    ) -> None:
        self._creds = initial
        self._allow_compose = allow_compose

    @property
    def allow_compose(self) -> bool:
        return self._allow_compose

    async def load(self) -> StoredCredentials | None:
        return self._creds

    async def load_for_revoke(self) -> StoredCredentials | None:
        return self._creds

    async def save(self, creds: StoredCredentials) -> None:
        lint_scopes(creds.scopes, allow_compose=self._allow_compose)
        self._creds = creds

    async def delete(self) -> None:
        self._creds = None


class PostgresTokenStore:
    """Refresh token + granted scopes, sealed with `DataCipher` (AES-256-GCM,
    AAD-bound) and kept in `system_flag` as `{"v": 1, "ct": <base64>}`. The
    scopes live INSIDE the ciphertext so they cannot be edited in place."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: DataCipher,
        *,
        allow_compose: bool = False,
    ) -> None:
        self._sf = session_factory
        self._cipher = cipher
        self._allow_compose = allow_compose

    @property
    def allow_compose(self) -> bool:
        return self._allow_compose

    async def save(self, creds: StoredCredentials) -> None:
        scopes = lint_scopes(creds.scopes, allow_compose=self._allow_compose)
        plaintext = json.dumps(
            {
                "refresh_token": creds.refresh_token.get_secret_value(),
                "scopes": sorted(scopes),
                "obtained_at": creds.obtained_at.astimezone(UTC).isoformat(),
            }
        )
        blob = self._cipher.encrypt(plaintext, aad=CREDENTIALS_AAD)
        value = {"v": _STORE_VERSION, "ct": base64.b64encode(blob).decode("ascii")}
        stmt = pg_insert(SystemFlag).values(
            key=CREDENTIALS_FLAG_KEY, value=value, updated_by="google_oauth"
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SystemFlag.key],
            set_={"value": value, "updated_by": "google_oauth", "updated_at": func.now()},
        )
        try:
            async with self._sf() as session, session.begin():
                await session.execute(stmt)
        except SQLAlchemyError:
            raise GoogleAuthError("token_store_error") from None

    async def load(self) -> StoredCredentials | None:
        raw = await self._read()
        if raw is None:
            return None
        return StoredCredentials(
            refresh_token=raw.refresh_token,
            scopes=lint_scopes(raw.scopes, allow_compose=self._allow_compose),
            obtained_at=raw.obtained_at,
        )

    async def load_for_revoke(self) -> StoredCredentials | None:
        """The stored record WITHOUT scope-lint: only for revoking it."""
        return await self._read()

    async def _read(self) -> StoredCredentials | None:
        try:
            async with self._sf() as session:
                row = await session.get(SystemFlag, CREDENTIALS_FLAG_KEY)
                value = dict(row.value) if row is not None else None
        except SQLAlchemyError:
            raise GoogleAuthError("token_store_error") from None
        if value is None:
            return None
        ct = value.get("ct")
        if value.get("v") != _STORE_VERSION or not isinstance(ct, str):
            raise GoogleAuthError("token_store_corrupt")
        try:
            blob = base64.b64decode(ct, validate=True)
        except (binascii.Error, ValueError):
            raise GoogleAuthError("token_store_corrupt") from None
        try:
            plaintext = self._cipher.decrypt_text(blob, aad=CREDENTIALS_AAD)
        except (CryptoError, UnicodeDecodeError):
            raise GoogleAuthError("token_decrypt_failed") from None
        data = _json_dict(plaintext.encode("utf-8"))
        try:
            assert data is not None
            refresh = data["refresh_token"]
            scopes = data["scopes"]
            obtained = datetime.fromisoformat(data["obtained_at"])
            if not isinstance(refresh, str) or not refresh or not isinstance(scopes, list):
                raise ValueError
            scope_set = frozenset(s for s in scopes if isinstance(s, str))
            if len(scope_set) != len(scopes):
                raise ValueError
        except (AssertionError, KeyError, TypeError, ValueError):
            raise GoogleAuthError("token_store_corrupt") from None
        return StoredCredentials(
            refresh_token=SecretStr(refresh), scopes=scope_set, obtained_at=obtained
        )

    async def delete(self) -> None:
        try:
            async with self._sf() as session, session.begin():
                await session.execute(
                    delete(SystemFlag).where(SystemFlag.key == CREDENTIALS_FLAG_KEY)
                )
        except SQLAlchemyError:
            raise GoogleAuthError("token_store_error") from None


# --------------------------------------------------------------------------
# Runtime: access tokens for the bot process
# --------------------------------------------------------------------------

_ACCESS_TOKEN_MARGIN_S = 60.0

REAUTH_MESSAGES: dict[str, str] = {
    "no_token": "Google is not connected yet. Run scripts/google_oauth.py authorize on your machine.",
    "invalid_grant": (
        "Google access expired or was revoked. Run scripts/google_oauth.py authorize "
        "on your machine to reconnect."
    ),
    "scope_violation": (
        "The stored Google grant has permissions Nimbus does not allow, so it is disabled. "
        "Run scripts/google_oauth.py revoke, then authorize again."
    ),
    "scope_missing": (
        "Google access is missing a permission this action needs. "
        "Run scripts/google_oauth.py authorize and keep all boxes ticked."
    ),
}
_DEFAULT_REAUTH_MESSAGE = "Google access needs to be re-authorised: run scripts/google_oauth.py."


@dataclass(frozen=True)
class ReauthNotice:
    code: str
    message: str


ReauthNotifier = Callable[[ReauthNotice], Awaitable[None]]


@dataclass(frozen=True)
class AuthStatus:
    has_token: bool
    scopes: frozenset[str]
    scope_ok: bool
    error: str | None = None


@dataclass
class _CachedAccess:
    token: SecretStr
    expires_at: float
    scopes: frozenset[str]
    fingerprint: str


def _fingerprint(token: SecretStr) -> str:
    return hashlib.sha256(token.get_secret_value().encode("utf-8")).hexdigest()[:16]


class GoogleAuth:
    """Hands out access tokens. Memory-only cache.

    Single-flight limits (by design, keep in mind when adding callers):
    * One ``asyncio.Lock`` serialises ALL calls, including the store read, so a
      slow refresh (up to ``request_timeout_s`` x attempts plus backoff, about
      45 s with defaults) delays every other Google caller in this process.
    * It is process-local. The CLI script and any second bot process refresh
      independently; Google tolerates this (refresh tokens are reusable), and a
      rotated refresh token is persisted by whoever sees it first.
    * The cache and the dead-token / notified state are in memory: after a
      restart a dead token is re-probed once and the user may be notified again
      (follow-up: persist this state).
    * The store is read on every call (cheap, and picks up re-authorisation done
      by the script without a restart).
    """

    def __init__(
        self,
        client: GoogleTokenClient,
        store: TokenStore,
        *,
        notifier: ReauthNotifier | None = None,
        clock: Callable[[], float] = time.monotonic,
        renotify_after_s: float = 24 * 3600.0,
    ) -> None:
        store_flag = getattr(store, "allow_compose", None)
        if not isinstance(store_flag, bool) or store_flag is not client.allow_compose:
            # One flag for both, or scope-lint disagrees between store and client.
            # A store that does not declare a bool flag is refused (fail closed).
            raise GoogleAuthError("compose_flag_mismatch")
        self._client = client
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._renotify_after_s = renotify_after_s
        self._lock = asyncio.Lock()
        self._cached: _CachedAccess | None = None
        self._dead: dict[str, str] = {}  # refresh-token fingerprint -> failure code
        self._notified: dict[tuple[str, str], float] = {}

    async def access_token(self, required_scopes: Iterable[str] = ()) -> SecretStr:
        required = frozenset(required_scopes)
        async with self._lock:
            fingerprint = ""
            try:
                creds = await self._store.load()
                if creds is None:
                    raise GoogleReauthRequired("no_token")
                fingerprint = _fingerprint(creds.refresh_token)
                if fingerprint in self._dead:
                    raise GoogleReauthRequired(self._dead[fingerprint])  # fail fast, no I/O
                try:
                    stored_scopes = lint_scopes(
                        creds.scopes, allow_compose=self._client.allow_compose
                    )
                except GoogleReauthRequired:
                    self._dead[fingerprint] = "scope_violation"
                    raise
                return await self._token_for(creds, stored_scopes, fingerprint, required)
            except GoogleReauthRequired as exc:
                await self._notify(exc.code, fingerprint)
                raise

    async def _token_for(
        self,
        creds: StoredCredentials,
        stored_scopes: frozenset[str],
        fingerprint: str,
        required: frozenset[str],
    ) -> SecretStr:
        cached = self._cached
        if (
            cached is not None
            and cached.fingerprint == fingerprint
            and self._clock() < cached.expires_at - _ACCESS_TOKEN_MARGIN_S
        ):
            return self._checked(cached, required)
        self._cached = None
        try:
            grant = await self._client.refresh(creds.refresh_token)
        except GoogleReauthRequired as exc:
            self._dead[fingerprint] = exc.code
            raise
        try:
            if grant.scopes is None:
                scopes = stored_scopes  # Google omitted it: keep exactly what was granted
            else:
                scopes = lint_scopes(grant.scopes, allow_compose=self._client.allow_compose)
                if not scopes <= stored_scopes:  # a refresh can never widen a grant
                    raise GoogleReauthRequired("scope_violation")
        except GoogleReauthRequired:
            self._dead[fingerprint] = "scope_violation"  # that token is never used
            raise
        new_fingerprint = fingerprint
        if grant.refresh_token is not None and grant.refresh_token != creds.refresh_token:
            await self._store.save(
                StoredCredentials(
                    refresh_token=grant.refresh_token,
                    scopes=scopes,
                    obtained_at=datetime.now(UTC),
                )
            )
            new_fingerprint = _fingerprint(grant.refresh_token)
        cached = _CachedAccess(
            token=grant.access_token,
            expires_at=self._clock() + grant.expires_in,
            scopes=scopes,
            fingerprint=new_fingerprint,
        )
        self._cached = cached
        return self._checked(cached, required)

    @staticmethod
    def _checked(cached: _CachedAccess, required: frozenset[str]) -> SecretStr:
        if not required <= cached.scopes:
            raise GoogleReauthRequired("scope_missing")
        return cached.token

    async def _notify(self, code: str, fingerprint: str) -> None:
        """Log (fixed code only) and notify, once per (code, token) per window."""
        key = (code, fingerprint)
        now = self._clock()
        last = self._notified.get(key)
        if last is not None and now - last < self._renotify_after_s:
            return
        self._notified[key] = now
        logger.warning("google auth reauth_required code=%s", code)
        if self._notifier is None:
            return
        notice = ReauthNotice(code=code, message=REAUTH_MESSAGES.get(code, _DEFAULT_REAUTH_MESSAGE))
        try:
            await self._notifier(notice)
        except Exception:  # noqa: BLE001 - a broken notifier must not mask the auth error
            logger.warning("google reauth notification failed code=%s", code)

    async def status(self) -> AuthStatus:
        try:
            creds = await self._store.load()
        except GoogleReauthRequired as exc:
            return AuthStatus(has_token=True, scopes=frozenset(), scope_ok=False, error=exc.code)
        if creds is None:
            return AuthStatus(has_token=False, scopes=frozenset(), scope_ok=False)
        try:
            lint_scopes(creds.scopes, allow_compose=self._client.allow_compose)
            ok = True
        except GoogleReauthRequired:
            ok = False
        return AuthStatus(has_token=True, scopes=creds.scopes, scope_ok=ok)

    async def revoke_and_forget(self) -> None:
        """Revoke at Google, then delete locally REGARDLESS of the remote
        outcome (raises `revoke_failed` afterwards so the operator revokes by
        hand at https://myaccount.google.com/permissions)."""
        async with self._lock:
            remote_failed = False
            found = getattr(self._store, "load_for_revoke", None)
            loader: RevokeLoader | None = found if callable(found) else None
            try:
                # Unlinted read: an out-of-policy token is exactly the one that
                # most needs revoking at Google.
                creds = await (loader() if loader is not None else self._store.load())
            except GoogleAuthError:
                # Unreadable (wrong key, corrupt; or out-of-policy on a store
                # without `load_for_revoke`): forget it locally, but we could
                # not revoke it at Google, so say so.
                creds = None
                remote_failed = True
            if creds is not None:
                try:
                    await self._client.revoke(creds.refresh_token)
                except GoogleAuthError:
                    remote_failed = True
            await self._store.delete()
            self._cached = None
            self._dead.clear()
            logger.info("google oauth token revoked and forgotten")
            if remote_failed:
                raise GoogleAuthError("revoke_failed")
