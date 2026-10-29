# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The single outbound HTTP choke point (ADR-0004, R13, R6, INV-7).

`src/net` is meant to be the only package that imports an HTTP transport.
Callers never pass a URL: they pass a `Service` and a path, and the host is
resolved from the process allowlist. Defence is layered:

1. `EgressClient.request` validates service, method, path, headers and deny
   rules *before building a request*. A denial raises `EgressDenied` and makes
   zero transport calls.
2. Every request, including same-host redirect hops and requests made by an
   SDK through `build_sdk_http_client`, then passes `_GuardTransport`, which
   re-checks scheme/host/port/userinfo/Host header/deny rules on the final
   `httpx.Request` before the real transport sees it.

Other properties: https only on port 443; httpx's own redirect following is
off, redirects are followed manually only to the *same* host (max N, each hop
re-validated) and anything else raises `EgressRedirectBlocked`; env proxies and
.netrc are ignored (`trust_env=False`); bodies are streamed and abandoned past
`max_response_bytes` (post-decompression, so gzip bombs are capped too);
connect/read/write/pool timeouts plus an overall deadline.

Outbound content check (P4.4, ADR-0023): `set_outbound_check(fn)` (once) adds
an async check over each request's path (not for Telegram: its path holds the
bot token), query and body of every `request()`. The model provider is not
covered: it is reached only through `build_sdk_http_client`, and the model is
meant to see the planted canaries. Anything but `True`, or an error, raises
`EgressDenied("canary_blocked")` before any I/O.

INV-7: error messages and log lines carry only service, method, status, byte
count and a fixed reason code -- never URL, path, query, headers or body.
httpx/httpcore loggers are silenced because httpx logs the full request URL
at INFO, which for Telegram includes the bot token.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Self

import httpx

from src.net.errors import (
    EgressDenied,
    EgressError,
    EgressRedirectBlocked,
    EgressResponseTooLarge,
    EgressTimeout,
    EgressTransportError,
)
from src.net.policy import (
    Allowlist,
    DenyRule,
    Service,
    all_rules,
    apply_deny_rules,
    check_headers,
    check_method,
    check_query,
    normalise_path,
)
from src.net.settings import HARD_MAX_RESPONSE_BYTES, HARD_MAX_TOTAL_TIMEOUT_S, NetSettings

logger = logging.getLogger("nimbus.net")

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_USER_AGENT = "nimbus/0.1"

QueryValue = str | int | float | bool | None
QueryParams = Mapping[str, QueryValue | Sequence[QueryValue]]
# (service, request text) -> True when it may leave. See `set_outbound_check`.
OutboundCheck = Callable[[Service, str], Awaitable[bool]]


def _outbound_text(
    service: Service, path: str, params: object, json: Any, content: bytes | None
) -> str:
    parts = [] if service is Service.TELEGRAM else [path]
    if params is not None:
        parts.append(repr(params))
    if json is not None:
        parts.append(jsonlib.dumps(json, default=str, ensure_ascii=False))
    if content is not None:
        parts.append(bytes(content).decode("utf-8", errors="replace"))
    return "\n".join(parts)


class _DropAll(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


def _silence_transport_loggers() -> None:
    """httpx logs 'HTTP Request: <METHOD> <full URL>' at INFO and httpcore logs
    connection details at DEBUG. Both can carry tokens (INV-7). Our own
    sanitised line in `EgressClient` replaces them."""
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.CRITICAL + 1)
        if not any(isinstance(f, _DropAll) for f in lg.filters):
            lg.addFilter(_DropAll())


_silence_transport_loggers()


@dataclass(frozen=True)
class EgressResponse:
    """Transport-agnostic response so callers never need to import httpx.
    `content`/`headers` are excluded from repr: OAuth token responses and
    Set-Cookie headers are secrets (INV-7)."""

    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    content: bytes = field(repr=False)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.content)


def _param_pairs(params: object, *, service: Service, method: str) -> list[tuple[str, object]]:
    """Flatten the `params` mapping; any other shape is refused (fail closed)."""
    if params is None:
        return []
    if not isinstance(params, Mapping):
        raise EgressDenied("bad_params", service=service.value, method=method)
    pairs: list[tuple[str, object]] = []
    for key, value in params.items():
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            pairs.extend((key, v) for v in value)
        else:
            pairs.append((key, value))
    return pairs


def _split_raw_path(url: httpx.URL) -> str:
    return url.raw_path.decode("ascii", errors="replace").split("?", 1)[0]


class _GuardTransport(httpx.AsyncBaseTransport):
    """Last line of defence: refuses any request the allowlist does not cover,
    without ever calling the wrapped transport."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        allowlist: Allowlist,
        rules: Sequence[DenyRule],
    ) -> None:
        self._inner = inner
        self._allowlist = allowlist
        self._rules = tuple(rules)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        method = request.method.upper()
        service = self._allowlist.service_for_host(url.host)
        if service is None:
            raise EgressDenied("host_not_allowlisted", method=method)
        if url.scheme != "https" or url.port not in (None, 443) or url.userinfo:
            raise EgressDenied("bad_url", service=service.value, method=method)
        host_header = request.headers.get("host")
        if host_header is not None and host_header.lower() != url.host:
            raise EgressDenied("host_header_mismatch", service=service.value, method=method)
        check_method(method, service=service)
        normalised = normalise_path(_split_raw_path(url), service=service, method=method)
        check_query(url.params.multi_items(), service=service, method=method)
        apply_deny_rules(self._rules, service=service, method=method, normalised_path=normalised)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _timeout(settings: NetSettings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.connect_timeout_s,
        read=settings.read_timeout_s,
        write=settings.write_timeout_s,
        pool=settings.pool_timeout_s,
    )


def _default_transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(retries=0, verify=True, trust_env=False)


class EgressClient:
    """Async, allowlisted HTTP client. One instance per process is enough."""

    def __init__(
        self,
        settings: NetSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        extra_deny_rules: Sequence[DenyRule] = (),
    ) -> None:
        self._settings = settings if settings is not None else NetSettings()
        self._allowlist = Allowlist.from_settings(self._settings)
        self._rules = all_rules(extra_deny_rules)
        guard = _GuardTransport(
            transport if transport is not None else _default_transport(),
            self._allowlist,
            self._rules,
        )
        self._client = httpx.AsyncClient(
            transport=guard,
            follow_redirects=False,
            trust_env=False,
            timeout=_timeout(self._settings),
            headers={"user-agent": _USER_AGENT},
        )
        self._outbound_check: OutboundCheck | None = None

    @property
    def allowlist(self) -> Allowlist:
        return self._allowlist

    def set_outbound_check(self, check: OutboundCheck) -> None:
        """Install the outbound content check (P4.4). Once only: a second call
        raises, so the check cannot be swapped out later."""
        if self._outbound_check is not None:
            raise RuntimeError("outbound check already set")
        if not callable(check):
            raise TypeError("outbound check must be callable")
        self._outbound_check = check

    async def _check_outbound(
        self,
        service: Service,
        verb: str,
        path: str,
        params: object,
        json: Any,
        content: bytes | None,
    ) -> None:
        check = self._outbound_check
        if check is None:
            return
        try:
            ok = await check(service, _outbound_text(service, path, params, json, content))
        except Exception:  # noqa: BLE001 - fail closed
            ok = False
        if ok is not True:
            logger.warning(
                "egress denied service=%s method=%s reason=canary_blocked", service.value, verb
            )
            raise EgressDenied("canary_blocked", service=service.value, method=verb)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        service: Service,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        timeout_s: float | None = None,
        max_bytes: int | None = None,
    ) -> EgressResponse:
        """Send one request to `service`'s allowlisted host at `path`.

        Raises `EgressDenied` (before any I/O) for unknown/disabled services,
        bad method/path/headers, or a deny-rule hit; `EgressRedirectBlocked`
        for an off-host/non-https/too-long redirect chain;
        `EgressResponseTooLarge`, `EgressTimeout`, `EgressTransportError`."""
        host = self._allowlist.host_for(service)
        verb = check_method(method, service=service)
        normalised = normalise_path(path, service=service, method=verb)
        check_headers(headers, service=service, method=verb)
        check_query(
            _param_pairs(params, service=service, method=verb), service=service, method=verb
        )
        apply_deny_rules(self._rules, service=service, method=verb, normalised_path=normalised)
        if json is not None and content is not None:
            raise EgressDenied("ambiguous_body", service=service.value, method=verb)
        await self._check_outbound(service, verb, path, params, json, content)
        cap = self._cap(max_bytes)
        deadline = self._deadline(timeout_s)

        try:
            async with asyncio.timeout(deadline):
                resp = await self._send_following(
                    service,
                    host,
                    verb,
                    f"https://{host}{path}",
                    params=params,
                    headers=headers,
                    json=json,
                    content=content,
                    cap=cap,
                )
        except EgressError as exc:
            logger.warning(
                "egress failed service=%s method=%s reason=%s", service.value, verb, exc.reason
            )
            raise
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("egress failed service=%s method=%s reason=timeout", service.value, verb)
            raise EgressTimeout("timeout", service=service.value, method=verb) from None
        except Exception as exc:  # noqa: BLE001 -- sanitise any failure (INV-7)
            logger.warning(
                "egress failed service=%s method=%s reason=transport_error type=%s",
                service.value,
                verb,
                type(exc).__name__,
            )
            raise EgressTransportError(
                "transport_error", service=service.value, method=verb
            ) from None
        finally:
            self._client.cookies.clear()

        logger.info(
            "egress ok service=%s method=%s status=%d bytes=%d",
            service.value,
            verb,
            resp.status_code,
            len(resp.content),
        )
        return resp

    def _cap(self, max_bytes: int | None) -> int:
        if max_bytes is None:
            return self._settings.max_response_bytes
        if max_bytes <= 0 or max_bytes > HARD_MAX_RESPONSE_BYTES:
            raise EgressDenied("bad_max_bytes")
        return max_bytes

    def _deadline(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return self._settings.total_timeout_s
        if timeout_s <= 0 or timeout_s > HARD_MAX_TOTAL_TIMEOUT_S:
            raise EgressDenied("bad_timeout")
        return timeout_s

    async def _send_following(
        self,
        service: Service,
        host: str,
        method: str,
        url: str,
        *,
        params: QueryParams | None,
        headers: Mapping[str, str] | None,
        json: Any,
        content: bytes | None,
        cap: int,
    ) -> EgressResponse:
        hops = 0
        while True:
            status, location, resp = await self._send_once(
                method,
                url,
                params=params,
                headers=headers,
                json=json,
                content=content,
                cap=cap,
            )
            if location is None:
                assert resp is not None
                return resp
            hops += 1
            if hops > self._settings.max_redirects:
                raise EgressRedirectBlocked(
                    "too_many_redirects", service=service.value, method=method
                )
            url = self._redirect_target(service, host, method, url, location)
            params = None  # the Location already carries any query
            if status == 303 or (status in (301, 302) and method == "POST"):
                method = "GET" if method != "HEAD" else "HEAD"
                json = None
                content = None

    def _redirect_target(
        self, service: Service, host: str, method: str, current: str, location: str
    ) -> str:
        try:
            target = httpx.URL(current).join(location)
        except (httpx.InvalidURL, ValueError, TypeError):
            raise EgressRedirectBlocked(
                "bad_redirect", service=service.value, method=method
            ) from None
        if (
            target.scheme != "https"
            or target.host != host
            or target.port not in (None, 443)
            or target.userinfo
        ):
            raise EgressRedirectBlocked("redirect_off_host", service=service.value, method=method)
        normalised = normalise_path(_split_raw_path(target), service=service, method=method)
        apply_deny_rules(self._rules, service=service, method=method, normalised_path=normalised)
        return str(target)

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        params: QueryParams | None,
        headers: Mapping[str, str] | None,
        json: Any,
        content: bytes | None,
        cap: int,
    ) -> tuple[int, str | None, EgressResponse | None]:
        async with self._client.stream(
            method,
            url,
            params=params,
            headers=dict(headers) if headers else None,
            json=json,
            content=content,
        ) as resp:
            location = resp.headers.get("location")
            if resp.status_code in _REDIRECT_STATUSES and location:
                return resp.status_code, location, None
            declared = resp.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > cap:
                raise EgressResponseTooLarge("response_too_large", method=method)
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > cap:
                    raise EgressResponseTooLarge("response_too_large", method=method)
            return (
                resp.status_code,
                None,
                EgressResponse(
                    status_code=resp.status_code,
                    headers=MappingProxyType(dict(resp.headers.items())),
                    content=bytes(buf),
                ),
            )


def build_sdk_http_client(
    service: Service,
    settings: NetSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    extra_deny_rules: Sequence[DenyRule] = (),
) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` for an SDK that insists on owning the client
    (the `openai` SDK's `http_client=`, ADR-0004). It is pinned to ONE
    service's host via `_GuardTransport`, never follows redirects, ignores env
    proxies, and silences transport logs. Note: the SDK reads the response
    itself, so the response-size cap does not apply on this path."""
    resolved = settings if settings is not None else NetSettings()
    full = Allowlist.from_settings(resolved)
    pinned = Allowlist(hosts={service: full.host_for(service)})
    guard = _GuardTransport(
        transport if transport is not None else _default_transport(),
        pinned,
        all_rules(extra_deny_rules),
    )
    return httpx.AsyncClient(
        transport=guard,
        follow_redirects=False,
        trust_env=False,
        timeout=_timeout(resolved),
        headers={"user-agent": _USER_AGENT},
    )
