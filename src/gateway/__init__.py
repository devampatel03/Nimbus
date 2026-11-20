# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Telegram gateway (long polling, allowlist, persisted offset, rate limit)."""

from src.gateway.protocols import Gateway
from src.gateway.ratelimit import RateLimiter
from src.gateway.settings import GatewaySettings
from src.gateway.store import UpdateStore
from src.gateway.telegram import TelegramGateway
from src.gateway.types import (
    ApprovalCard,
    FileRef,
    InboundUpdate,
    MessageRef,
    TelegramError,
    UpdateKind,
)

__all__ = [
    "ApprovalCard",
    "FileRef",
    "Gateway",
    "GatewaySettings",
    "InboundUpdate",
    "MessageRef",
    "RateLimiter",
    "TelegramError",
    "TelegramGateway",
    "UpdateKind",
    "UpdateStore",
]
