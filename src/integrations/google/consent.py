# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""One-time Google consent flow: PKCE, loopback receiver, code exchange (P3.4).

This module is imported ONLY by ``scripts/google_oauth.py`` and runs on the
operator's machine. It is the only code in ``src/`` that opens a listening
socket, so it must never be imported by the bot (R3: the app has no inbound
port). ``tests/unit/integrations/google/test_auth_consent_isolation.py`` fails
if any ``src/`` module imports it or starts a server elsewhere.

The runtime side (access tokens, store, scope-lint, token endpoint client) is
in ``auth.py``. The browser, not this process, talks to ``accounts.google.com``;
this process only reaches ``oauth2.googleapis.com`` (``POST /token`` and
``POST /revoke``) through ``src.net``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from pydantic import SecretStr

from src.integrations.google.auth import (
    GoogleAuthError,
    GoogleOAuthSettings,
    GoogleReauthRequired,
    GoogleTokenClient,
    StoredCredentials,
    TokenStore,
    lint_scopes,
)
from src.net import EgressClient

logger = logging.getLogger("nimbus.integrations.google.auth")

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
    allow_compose: bool = False,
) -> str:
    requested = lint_scopes(scopes, allow_compose=allow_compose)
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
# Loopback receiver
# --------------------------------------------------------------------------

CALLBACK_PATH = "/oauth2callback"
_LOOPBACK_HOST = "127.0.0.1"  # literal on purpose: never 0.0.0.0, never configurable
_MAX_REQUEST_BYTES = 8192

_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Nimbus</title>"
    b"<p>Authorisation received. You can close this tab and return to the terminal.</p>"
)
_REJECT_PAGE = b"<!doctype html><p>Request rejected.</p>"


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
    """One-shot HTTP listener on 127.0.0.1.

    Only a request that is fully valid ENDS the flow: right Host header, GET,
    an origin-form target on the callback path, a ``state`` equal (constant
    time) to ``expected_state``, and a ``code`` or ``error``. Anything else
    (stray browser probes, foreign Host, wrong path, forged state) is answered
    4xx and ignored, so a local nuisance cannot burn the one-shot. When the
    valid request arrives the listening socket is closed at once, so a second
    request is refused. A connection that never sends a request line does not
    count (it times out after ``request_read_timeout_s``). The overall wait is
    bounded by ``timeout_s``. Nothing from the request is echoed back."""

    def __init__(
        self,
        *,
        expected_state: str,
        timeout_s: float = 300.0,
        port: int = 0,
        request_read_timeout_s: float = 5.0,
    ) -> None:
        self._expected_state = expected_state
        self._timeout_s = timeout_s
        self._want_port = port
        self._read_timeout_s = request_read_timeout_s
        self._server: asyncio.Server | None = None
        self._outcome: asyncio.Future[CallbackParams] | None = None
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
                params = await self._outcome
        except TimeoutError:
            if self._server is not None:
                self._server.close()
            raise GoogleAuthError("callback_timeout") from None
        return params

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
            status, reason, params = self._decide(raw)
            if params is not None:
                self._outcome.set_result(params)
                if self._server is not None:
                    self._server.close()
            writer.write(_response(status, reason, _PAGE if params is not None else _REJECT_PAGE))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def _decide(self, raw: bytes) -> tuple[int, str, CallbackParams | None]:
        """(status, reason, params). `params` is not None only for the one
        valid callback; every other request is rejected without effect."""
        try:
            head = raw.decode("latin-1").split("\r\n")
            method, target, _version = head[0].split(" ")
            headers: dict[str, str] = {}
            for line in head[1:]:
                if ":" in line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
        except ValueError:
            return 400, "Bad Request", None
        if headers.get("host") != f"{_LOOPBACK_HOST}:{self._port}":
            return 400, "Bad Request", None  # DNS-rebinding guard
        if method != "GET":
            return 405, "Method Not Allowed", None
        # origin-form only: reject absolute-form ('http://...') and '//host/...'
        if not target.startswith("/") or target.startswith("//"):
            return 400, "Bad Request", None
        try:
            parts = urlsplit(target)
            query = parse_qs(parts.query, max_num_fields=20)
        except ValueError:
            return 400, "Bad Request", None
        if parts.path != CALLBACK_PATH:
            return 404, "Not Found", None
        state = _first(query, "state")
        code = _first(query, "code")
        error = _first(query, "error")
        if state is None or not hmac.compare_digest(
            state.encode("utf-8"), self._expected_state.encode("utf-8")
        ):
            return 400, "Bad Request", None
        if code is None and error is None:
            return 400, "Bad Request", None
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
    the state check and scope-lint (against what was requested AND the
    effective allowed set) and carries a refresh token."""
    allow_compose = settings.allow_gmail_compose
    requested = lint_scopes(scopes, allow_compose=allow_compose)
    client_id, _ = settings.require_client()  # before any listener is opened
    token_client = client if client is not None else GoogleTokenClient(settings, egress)
    pkce = generate_pkce()
    state = secrets.token_urlsafe(32)
    async with LoopbackServer(expected_state=state, timeout_s=timeout_s, port=port) as server:
        present_url(
            build_authorization_url(
                client_id=client_id,
                redirect_uri=server.redirect_uri,
                scopes=requested,
                state=state,
                code_challenge=pkce.challenge,
                allow_compose=allow_compose,
            )
        )
        params = await server.wait()
        redirect_uri = server.redirect_uri
    # The server already enforced this; kept as an independent check.
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
        granted = lint_scopes(grant.scopes, allow_compose=allow_compose)
    except GoogleReauthRequired:
        logger.warning("google oauth grant rejected code=scope_violation")
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
    logger.info(
        "google oauth authorised scopes=%s",
        ",".join(s.rsplit("/", 1)[-1] for s in sorted(granted)),
    )
    return granted
