# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Adversarial (P4.5, R13): address forms that try to reach loopback, private
or cloud-metadata IPv4 through an IPv6 or zone-id spelling must be refused by
the post-DNS check. Table-driven, written independently of the implementation."""

from __future__ import annotations

import httpx
import pytest

from src.net.addrcheck import PublicAddressTransport
from src.net.errors import EgressDenied
from tests.unit.net.net_helpers import RecordingTransport

BYPASSES = [
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "64:ff9b:1::a9fe:a9fe",  # local-use NAT64
    "::127.0.0.1",  # IPv4-compatible
    "::a9fe:a9fe",
    "::ffff:0:a9fe:a9fe",  # IPv4-translated
    "::ffff:a9fe:a9fe",  # IPv4-mapped metadata
    "2002:a9fe:a9fe::1",  # 6to4 of metadata
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
    "169.254.169.254%eth0",  # zone id on IPv4
    "127.0.0.1%1",
    "fe80::1%eth0",
]


@pytest.mark.parametrize("addr", BYPASSES)
async def test_resolver_answer_is_refused(addr: str) -> None:
    inner = RecordingTransport()

    async def resolve(host: str, port: int) -> list[str]:
        return [addr]

    t = PublicAddressTransport(inner, resolve)
    with pytest.raises(EgressDenied):
        await t.handle_async_request(httpx.Request("GET", "https://api.telegram.org/x"))
    assert inner.calls == []
