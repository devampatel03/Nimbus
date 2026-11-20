# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Single outbound-HTTP choke point (ADR-0004). See `src.net.http`."""

from src.net.errors import (
    EgressConfigError,
    EgressDenied,
    EgressError,
    EgressRedirectBlocked,
    EgressResponseTooLarge,
    EgressTimeout,
    EgressTransportError,
)
from src.net.http import EgressClient, EgressResponse, build_sdk_http_client
from src.net.policy import DEFAULT_DENY_RULES, FIXED_HOSTS, Allowlist, DenyRule, Service
from src.net.settings import NetSettings

__all__ = [
    "DEFAULT_DENY_RULES",
    "FIXED_HOSTS",
    "Allowlist",
    "DenyRule",
    "EgressClient",
    "EgressConfigError",
    "EgressDenied",
    "EgressError",
    "EgressRedirectBlocked",
    "EgressResponse",
    "EgressResponseTooLarge",
    "EgressTimeout",
    "EgressTransportError",
    "NetSettings",
    "Service",
    "build_sdk_http_client",
]
