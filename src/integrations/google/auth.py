# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Google OAuth for Nimbus: one-time loopback consent flow, encrypted refresh
token store, and access-token refresh (P3.4; ADR-0005, ADR-0007, ADR-0004,
ADR-0019; R3, R6, R12, R13, R15; INV-7).

How it fits together
--------------------
* ``scripts/google_oauth.py authorize`` runs on the OPERATOR'S machine (never in
  the bot process, so the app opens no inbound port, R3). It starts a
  one-shot HTTP listener on ``127.0.0.1`` (ephemeral port), prints the consent
  URL, receives the redirect, checks ``state``, exchanges the code with PKCE
  (S256) and stores the refresh token.
* The refresh token is stored ONLY as AES-256-GCM ciphertext (``src/db/crypto``)
  inside the existing ``system_flag`` table (key ``google_oauth_credentials``),
  with an AAD that binds it to that row. No new table or migration.
* The bot process uses :class:`GoogleAuth` to turn the stored refresh token into
  short-lived access tokens (kept in memory only). Every request to Google goes
  through ``src.net`` (``Service.GOOGLE_OAUTH``: ``POST /token`` and
  ``POST /revoke`` on ``oauth2.googleapis.com`` only). This module imports no
  HTTP transport itself (INV-1). The browser, not this process, talks to
  ``accounts.google.com``.

Scopes (exact, enforced; see ``ALLOWED_SCOPES``)
------------------------------------------------
``calendar.events``, ``gmail.readonly``, ``gmail.compose`` (ADR-0007 proposed
default: drafts; Gmail has no drafts-only scope, so the send block is enforced
in code and in ``src.net``, not by OAuth), ``drive.readonly`` (H17). Anything
else -- the Gmail send and modify scopes, the full-mailbox scope, full Drive,
full Calendar -- is rejected: in the consent URL we build, in what Google
reports as granted at code exchange and at every refresh, and in what we read
back from the store. A token that reports an extra scope is never used (and is
revoked best-effort at consent time). Granting a SUBSET is fine (Google's
granular consent lets the user untick scopes); callers pass ``required_scopes``
to :meth:`GoogleAuth.access_token`.

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
import hmac
import json
import logging
import random
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, Self
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from pydantic import Field, SecretStr
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

ALLOWED_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_CALENDAR_EVENTS,
        SCOPE_GMAIL_READONLY,
        SCOPE_GMAIL_COMPOSE,
        SCOPE_DRIVE_READONLY,
    }
)
DEFAULT_SCOPES: tuple[str, ...] = tuple(sorted(ALLOWED_SCOPES))


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


def lint_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Return `scopes` as a set if it is a non-empty subset of ALLOWED_SCOPES;
    otherwise raise `GoogleReauthRequired("scope_violation")`. The offending
    scope is deliberately not put in the error."""
    granted = frozenset(scopes)
    if not granted or not granted <= ALLOWED_SCOPES:
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
    max_retries: int = Field(default=2, ge=0, le=5)
    backoff_base_s: float = Field(default=0.5, ge=0, le=30)
    backoff_max_s: float = Field(default=8.0, ge=0, le=120)
    request_timeout_s: float = Field(default=15.0, gt=0, le=60)

    def require_client(self) -> tuple[SecretStr, SecretStr]:
        if self.client_id is None or self.client_secret is None:
            raise GoogleAuthError("client_not_configured")
        if not self.client_id.get_secret_value() or not self.client_secret.get_secret_value():
            raise GoogleAuthError("client_not_configured")
        return self.client_id, self.client_secret


# --------------------------------------------------------------------------
# PKCE (RFC 7636, S256)
# --------------------------------------------------------------------------


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PkcePair:
    verifier: SecretStr
    challenge: str


def generate_pkce() -> PkcePair:
    verifier = secrets.token_urlsafe(64)  # 86 chars of [A-Za-z0-9_-]; RFC range is 43..128
    return PkcePair(verifier=SecretStr(verifier), challenge=pkce_challenge(verifier))


_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"  # opened by the BROWSER only


def build_authorization_url(
    *,
    client_id: str | SecretStr,
    redirect_uri: str,
    scopes: Iterable[str],
    state: str,
    code_challenge: str,
) -> str:
    requested = lint_scopes(scopes)
    cid = client_id.get_secret_value() if isinstance(client_id, SecretStr) else client_id
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "scope": " ".join(sorted(requested)),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",  # we need a refresh token
        "prompt": "consent",  # Google only re-issues a refresh token with consent
        "include_granted_scopes": "false",  # never fold in previously granted scopes
    }
    return f"{_AUTH_ENDPOINT}?{urlencode(params, quote_via=quote)}"


# --------------------------------------------------------------------------
# Loopback receiver (runs only inside scripts/google_oauth.py)
# --------------------------------------------------------------------------

CALLBACK_PATH = "/oauth2callback"
_LOOPBACK_HOST = "127.0.0.1"  # literal on purpose: never 0.0.0.0, never configurable
_MAX_REQUEST_BYTES = 8192

_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Nimbus</title>"
    b"<p>Authorisation received. You can close this tab and return to the terminal.</p>"
)


@dataclass(frozen=True)
class CallbackParams:
    state: str | None
    code: SecretStr | None = field(default=None)
    error: str | None = None


def _response(status: int, reason: str, body: bytes) -> bytes:
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "Content-Security-Policy: default-src 'none'\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("ascii") + body


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


class LoopbackServer:
    """One-shot HTTP listener on 127.0.0.1. The FIRST well-formed request
    (right Host header, GET, callback path) ends the flow; the listening socket
    is closed as soon as it is read, so a second request is refused. A
    connection that never sends a request line does not count (it times out
    after `request_read_timeout_s`). The overall wait is bounded by `timeout_s`.
    Nothing from the request (code, state) is echoed back to the browser."""

    def __init__(
        self,
        *,
        timeout_s: float = 300.0,
        port: int = 0,
        request_read_timeout_s: float = 5.0,
    ) -> None:
        self._timeout_s = timeout_s
        self._want_port = port
        self._read_timeout_s = request_read_timeout_s
        self._server: asyncio.Server | None = None
        self._outcome: asyncio.Future[CallbackParams | str] | None = None
        self._port = 0

    async def __aenter__(self) -> Self:
        loop = asyncio.get_running_loop()
        self._outcome = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle, host=_LOOPBACK_HOST, port=self._want_port, limit=_MAX_REQUEST_BYTES
        )
        self._port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._close()

    @property
    def port(self) -> int:
        return self._port

    @property
    def redirect_uri(self) -> str:
        return f"http://{_LOOPBACK_HOST}:{self._port}{CALLBACK_PATH}"

    @property
    def bound_hosts(self) -> list[str]:
        assert self._server is not None
        return [str(s.getsockname()[0]) for s in self._server.sockets]

    async def _close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        try:
            async with asyncio.timeout(2):
                await server.wait_closed()
        except TimeoutError:
            pass

    async def wait(self) -> CallbackParams:
        assert self._outcome is not None
        try:
            async with asyncio.timeout(self._timeout_s):
                outcome = await self._outcome
        except TimeoutError:
            outcome = "callback_timeout"
        if self._server is not None:
            self._server.close()  # stop accepting at once; full cleanup in __aexit__
        if isinstance(outcome, str):
            raise GoogleAuthError(outcome)
        return outcome

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=self._read_timeout_s
                )
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                return  # no usable request: does not consume the one-shot
            assert self._outcome is not None
            if self._outcome.done():
                return
            status, reason, outcome = self._decide(raw)
            self._outcome.set_result(outcome)
            if self._server is not None:
                self._server.close()
            body = _PAGE if status == 200 else b"<!doctype html><p>Request rejected.</p>"
            writer.write(_response(status, reason, body))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def _decide(self, raw: bytes) -> tuple[int, str, CallbackParams | str]:
        try:
            head = raw.decode("latin-1").split("\r\n")
            method, target, _version = head[0].split(" ")
            headers = {}
            for line in head[1:]:
                if ":" in line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
        except ValueError:
            return 400, "Bad Request", "bad_callback"
        if headers.get("host") != f"{_LOOPBACK_HOST}:{self._port}":
            return 400, "Bad Request", "bad_callback"  # DNS-rebinding guard
        if method != "GET":
            return 405, "Method Not Allowed", "bad_callback"
        try:
            parts = urlsplit(target)
            query = parse_qs(parts.query, max_num_fields=20)
        except ValueError:
            return 400, "Bad Request", "bad_callback"
        if parts.path != CALLBACK_PATH:
            return 404, "Not Found", "bad_callback"
        state = _first(query, "state")
        code = _first(query, "code")
        error = _first(query, "error")
        if code is None and error is None:
            return 400, "Bad Request", "bad_callback"
        if error is not None and not error.replace("_", "").isalnum():
            error = "unknown"
        return (
            200,
            "OK",
            CallbackParams(
                state=state,
                code=SecretStr(code) if code is not None else None,
                error=error[:64] if error is not None else None,
            ),
        )


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
    async def load(self) -> StoredCredentials | None: ...
    async def save(self, creds: StoredCredentials) -> None: ...
    async def delete(self) -> None: ...


class InMemoryTokenStore:
    """Test / dry-run store. `initial` is deliberately not linted so tests can
    seed a hostile record; `save` lints like the real store."""

    def __init__(self, initial: StoredCredentials | None = None) -> None:
        self._creds = initial

    async def load(self) -> StoredCredentials | None:
        return self._creds

    async def save(self, creds: StoredCredentials) -> None:
        lint_scopes(creds.scopes)
        self._creds = creds

    async def delete(self) -> None:
        self._creds = None


class PostgresTokenStore:
    """Refresh token + granted scopes, sealed with `DataCipher` (AES-256-GCM,
    AAD-bound) and kept in `system_flag` as `{"v": 1, "ct": <base64>}`. The
    scopes live INSIDE the ciphertext so they cannot be edited in place."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], cipher: DataCipher
    ) -> None:
        self._sf = session_factory
        self._cipher = cipher

    async def save(self, creds: StoredCredentials) -> None:
        scopes = lint_scopes(creds.scopes)
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
            refresh_token=SecretStr(refresh),
            scopes=lint_scopes(scope_set),
            obtained_at=obtained,
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
    """Hands out access tokens. Single-flight (one lock), memory-only cache."""

    def __init__(
        self,
        client: GoogleTokenClient,
        store: TokenStore,
        *,
        notifier: ReauthNotifier | None = None,
        clock: Callable[[], float] = time.monotonic,
        renotify_after_s: float = 24 * 3600.0,
    ) -> None:
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
                    stored_scopes = lint_scopes(creds.scopes)
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
            scopes = lint_scopes(grant.scopes if grant.scopes is not None else stored_scopes)
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
        if self._notifier is None:
            return
        key = (code, fingerprint)
        now = self._clock()
        last = self._notified.get(key)
        if last is not None and now - last < self._renotify_after_s:
            return
        self._notified[key] = now
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
            lint_scopes(creds.scopes)
            ok = True
        except GoogleReauthRequired:
            ok = False
        return AuthStatus(has_token=True, scopes=creds.scopes, scope_ok=ok)

    async def revoke_and_forget(self) -> None:
        """Revoke at Google, then delete locally REGARDLESS of the remote
        outcome (raises `revoke_failed` afterwards so the operator revokes by
        hand at https://myaccount.google.com/permissions)."""
        async with self._lock:
            try:
                creds = await self._store.load()
            except GoogleAuthError:
                creds = None  # unreadable record: just forget it
            remote_failed = False
            if creds is not None:
                try:
                    await self._client.revoke(creds.refresh_token)
                except GoogleAuthError:
                    remote_failed = True
            await self._store.delete()
            self._cached = None
            self._dead.clear()
            if remote_failed:
                raise GoogleAuthError("revoke_failed")


# --------------------------------------------------------------------------
# One-time interactive authorisation (operator machine)
# --------------------------------------------------------------------------


async def authorize_interactive(
    *,
    settings: GoogleOAuthSettings,
    egress: EgressClient,
    store: TokenStore,
    scopes: Iterable[str],
    present_url: Callable[[str], object],
    timeout_s: float = 300.0,
    port: int = 0,
    client: GoogleTokenClient | None = None,
) -> frozenset[str]:
    """Run the consent flow once and persist the refresh token. Returns the
    scopes Google actually granted. Nothing is stored unless the grant passes
    state check, scope-lint and carries a refresh token."""
    requested = lint_scopes(scopes)
    client_id, _ = settings.require_client()  # before any listener is opened
    token_client = client if client is not None else GoogleTokenClient(settings, egress)
    pkce = generate_pkce()
    state = secrets.token_urlsafe(32)
    async with LoopbackServer(timeout_s=timeout_s, port=port) as server:
        present_url(
            build_authorization_url(
                client_id=client_id,
                redirect_uri=server.redirect_uri,
                scopes=requested,
                state=state,
                code_challenge=pkce.challenge,
            )
        )
        params = await server.wait()
        redirect_uri = server.redirect_uri
    if params.state is None or not hmac.compare_digest(
        params.state.encode("utf-8"), state.encode("utf-8")
    ):
        raise GoogleAuthError("state_mismatch")
    if params.error is not None:
        raise GoogleAuthError(
            "authorization_denied" if params.error == "access_denied" else "authorization_failed"
        )
    if params.code is None:
        raise GoogleAuthError("bad_callback")
    grant = await token_client.exchange_code(params.code, pkce.verifier, redirect_uri)
    try:
        if grant.scopes is None or not grant.scopes <= requested:
            raise GoogleReauthRequired("scope_violation")
        granted = lint_scopes(grant.scopes)
    except GoogleReauthRequired:
        victim = grant.refresh_token if grant.refresh_token is not None else grant.access_token
        try:
            await token_client.revoke(victim)  # best effort: do not leave it live
        except GoogleAuthError:
            logger.warning("google revoke of rejected grant failed")
        raise
    if grant.refresh_token is None:
        raise GoogleAuthError("no_refresh_token")
    await store.save(
        StoredCredentials(
            refresh_token=grant.refresh_token, scopes=granted, obtained_at=datetime.now(UTC)
        )
    )
    return granted
