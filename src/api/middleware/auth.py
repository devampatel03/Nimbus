# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""JWT guard applied to every HTTP and WebSocket route.

A raw ASGI middleware (not `starlette.middleware.base.BaseHTTPMiddleware`,
which only ever sees `http`-scope traffic) so the same class can also
reject an unauthenticated WebSocket handshake before `websocket.accept`
is ever sent -- an unauthorized client never gets a live socket, it just
sees the handshake refused.

Exempt paths: `/health` (ops probe, no user data), `/docs`/`/openapi.json`/
`/redoc` (API docs, harmless to expose on a local/private network), and
`/auth/token` (the login endpoint itself -- can't require a token to get
a token).

Token lookup: `Authorization: Bearer <token>` header for both HTTP and
WebSocket. WebSocket also accepts `?token=<token>` as a fallback, since
browser `WebSocket` clients cannot set custom handshake headers -- only
Bearer-token content ever appears here, never the login password.
"""

from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.api.auth import TokenError, decode_access_token

EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc", "/auth/token"})


class JWTAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        token = self._extract_token(scope)
        if token is not None:
            try:
                decode_access_token(token)
            except TokenError:
                token = None

        if token is None:
            await self._reject(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _extract_token(scope: Scope) -> str | None:
        headers: dict[bytes, bytes] = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization")
        if auth_header:
            value: str = auth_header.decode("latin-1")
            if value.lower().startswith("bearer "):
                token: str = value[len("bearer ") :].strip()
                return token
        if scope["type"] == "websocket":
            query_bytes: bytes = scope.get("query_string", b"")
            tokens: list[str] = parse_qs(query_bytes.decode("utf-8")).get("token", [])
            if tokens:
                return tokens[0]
        return None

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Refused before `websocket.accept` -- the client never gets a live socket.
            await send({"type": "websocket.close", "code": 4401})
        else:
            response = JSONResponse({"detail": "not authenticated"}, status_code=401)
            await response(scope, receive, send)
