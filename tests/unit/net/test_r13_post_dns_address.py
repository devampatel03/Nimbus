# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.5: post-DNS non-public address block (R13). No real DNS or socket."""

from __future__ import annotations

import httpx
import pytest

from src.net.addrcheck import PublicAddressTransport, Resolver, is_public_address
from src.net.errors import EgressDenied, EgressTransportError
from tests.unit.net.net_helpers import RecordingTransport

NON_PUBLIC = [
    "127.0.0.1",
    "10.0.0.5",
    "172.16.3.4",
    "192.168.1.1",
    "169.254.169.254",
    "169.254.1.1",
    "0.0.0.0",
    "100.64.0.1",
    "::1",
    "fe80::1",
    "fc00::1",
    "fd00:ec2::254",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "224.0.0.1",
    "64:ff9b::a9fe:a9fe",
    "64:ff9b:1::a9fe:a9fe",
    "::127.0.0.1",
    "::a9fe:a9fe",
    "::ffff:0:a9fe:a9fe",
    "2002:a9fe:a9fe::1",
    "169.254.169.254%eth0",
    "127.0.0.1%1",
    "not-an-ip",
    "",
]


@pytest.mark.parametrize("addr", NON_PUBLIC)
def test_non_public_rejected(addr: str) -> None:
    assert is_public_address(addr) is False


@pytest.mark.parametrize("addr", ["149.154.167.220", "142.250.80.10", "2607:f8b0:4004::200e"])
def test_public_accepted(addr: str) -> None:
    assert is_public_address(addr) is True


def _req(host: str = "api.telegram.org") -> httpx.Request:
    return httpx.Request("GET", f"https://{host}/x")


def _resolver(addrs: list[str]) -> Resolver:
    async def resolve(host: str, port: int) -> list[str]:
        return addrs

    return resolve


async def test_private_resolution_blocked_before_inner() -> None:
    inner = RecordingTransport()
    t = PublicAddressTransport(inner, _resolver(["169.254.169.254"]))
    with pytest.raises(EgressDenied) as e:
        await t.handle_async_request(_req())
    assert e.value.reason == "non_public_address"
    assert "169.254" not in str(e.value) and "telegram" not in str(e.value)
    assert inner.calls == []


async def test_any_private_among_many_blocks() -> None:
    inner = RecordingTransport()
    t = PublicAddressTransport(inner, _resolver(["149.154.167.220", "10.0.0.1"]))
    with pytest.raises(EgressDenied):
        await t.handle_async_request(_req())
    assert inner.calls == []


async def test_public_resolution_passes() -> None:
    inner = RecordingTransport()
    t = PublicAddressTransport(inner, _resolver(["149.154.167.220"]))
    resp = await t.handle_async_request(_req())
    assert resp.status_code == 200 and len(inner.calls) == 1


async def test_ip_literal_host_checked_without_resolver() -> None:
    inner = RecordingTransport()

    async def boom(host: str, port: int) -> list[str]:
        raise AssertionError("resolver must not be called for a literal")

    t = PublicAddressTransport(inner, boom)
    with pytest.raises(EgressDenied):
        await t.handle_async_request(httpx.Request("GET", "https://127.0.0.1/x"))
    assert inner.calls == []


async def test_dns_failure_and_empty_fail_closed() -> None:
    inner = RecordingTransport()

    async def fail(host: str, port: int) -> list[str]:
        raise OSError("nxdomain secret-host")

    with pytest.raises(EgressTransportError) as e:
        await PublicAddressTransport(inner, fail).handle_async_request(_req())
    assert "secret-host" not in str(e.value)
    with pytest.raises(EgressTransportError):
        await PublicAddressTransport(inner, _resolver([])).handle_async_request(_req())
    assert inner.calls == []


def test_default_transport_is_wrapped() -> None:
    from src.net.http import _default_transport

    assert isinstance(_default_transport(), PublicAddressTransport)
