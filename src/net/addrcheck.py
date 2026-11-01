# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Post-DNS address check (P4.5, R13; open item from P1.4a/b).

The host allowlist is by name. A name on the allowlist can still resolve to a
loopback, private, link-local or cloud-metadata address (DNS rebinding, a
hijacked resolver, a hostile record). `PublicAddressTransport` resolves the
host just before the real transport connects and refuses the request if ANY
returned address is not globally routable. Cloud metadata (169.254.169.254) is
link-local and so is covered; it is also listed explicitly.

Honest limit: this is check-then-connect. The transport resolves again when it
opens the socket, so a resolver that answers differently the second time
(rebinding inside the TTL race) is not closed by this alone. The host-level
nftables rule (deploy/nftables/) is the backstop for that race: it drops
private and metadata destinations regardless of DNS. No new host or Service is
added here; this only narrows R13.

Errors carry only a fixed reason code (INV-7): never the host or address.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable

import httpx

from src.net.errors import EgressDenied, EgressTransportError

Resolver = Callable[[str, int], Awaitable[list[str]]]

_METADATA = frozenset(
    {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
)


def is_public_address(raw: str) -> bool:
    """True only for a globally routable unicast address. Fails closed on
    anything unparsable. IPv4-mapped IPv6 is judged by its embedded IPv4."""
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA:
        return False
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local:
        return False
    return ip.is_global


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=1)  # SOCK_STREAM
    return [str(info[4][0]) for info in infos]


class PublicAddressTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, resolver: Resolver | None = None) -> None:
        self._inner = inner
        self._resolver = resolver if resolver is not None else system_resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        method = request.method.upper()
        try:
            addrs = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addrs = await self._resolver(host, request.url.port or 443)
            except OSError:
                raise EgressTransportError("dns_failed", method=method) from None
            if not addrs:
                raise EgressTransportError("dns_failed", method=method)
        if not all(is_public_address(a) for a in addrs):
            raise EgressDenied("non_public_address", method=method)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
