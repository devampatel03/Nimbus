# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Thin Bot API client over `src.net.EgressClient` (Service.TELEGRAM).

This module imports no HTTP library (INV-1 / ADR-0004). The token appears only
in the request path handed to the egress client; every error raised from here
is built from fixed reason codes and numeric status, never from the URL, so the
token cannot reach a log or traceback (INV-7). Egress errors are re-raised
unchanged (they are already sanitised).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from src.gateway.settings import GatewaySettings
from src.gateway.types import (
    FileTooLarge,
    TelegramAuthError,
    TelegramError,
    TelegramRateLimited,
)
from src.net import EgressClient, EgressResponseTooLarge, Service

logger = logging.getLogger("nimbus.gateway")

_FILE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,255}$")


class TelegramApi:
    def __init__(
        self,
        egress: EgressClient,
        settings: GatewaySettings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_retry_after: Callable[[float], None] | None = None,
    ) -> None:
        self._egress = egress
        self._s = settings
        self._sleep = sleep
        self._on_retry_after = on_retry_after

    def _prefix(self) -> str:
        return f"/bot{self._s.bot_token.get_secret_value()}"

    async def call(
        self, method: str, payload: dict[str, Any], *, timeout_s: float | None = None
    ) -> Any:
        """POST a Bot API method; returns `result`. Handles 429 `retry_after`
        by sleeping and retrying (bounded)."""
        attempts = 0
        while True:
            resp = await self._egress.request(
                Service.TELEGRAM,
                "POST",
                f"{self._prefix()}/{method}",
                json=payload,
                timeout_s=timeout_s,
            )
            body: Any = None
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.is_success and isinstance(body, dict) and body.get("ok") is True:
                return body.get("result")
            code = resp.status_code
            params = body.get("parameters") if isinstance(body, dict) else None
            if code == 429:
                retry_after = params.get("retry_after") if isinstance(params, dict) else None
                if not isinstance(retry_after, int | float) or isinstance(retry_after, bool):
                    retry_after = 1
                if retry_after > self._s.max_retry_after_s or attempts >= self._s.max_429_retries:
                    raise TelegramRateLimited("rate_limited", code=429)
                attempts += 1
                logger.warning("telegram 429 method=%s retry_after=%s", method, retry_after)
                if self._on_retry_after is not None:
                    self._on_retry_after(float(retry_after))
                await self._sleep(float(retry_after))
                continue
            if code in (401, 404):
                raise TelegramAuthError("unauthorized", code=code)
            raise TelegramError(
                "api_error" if isinstance(body, dict) else "bad_response", code=code
            )

    async def get_updates(self, offset: int | None) -> list[Any]:
        payload: dict[str, Any] = {
            "timeout": self._s.poll_timeout_s,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self.call(
            "getUpdates", payload, timeout_s=float(self._s.poll_timeout_s) + 15.0
        )
        return result if isinstance(result, list) else []

    async def download(self, file_id: str, *, max_bytes: int) -> bytes:
        info = await self.call("getFile", {"file_id": file_id})
        if not isinstance(info, dict):
            raise TelegramError("bad_response")
        size = info.get("file_size")
        if isinstance(size, int) and size > max_bytes:
            raise FileTooLarge("file_too_large")
        path = info.get("file_path")
        if not isinstance(path, str) or not _FILE_PATH_RE.fullmatch(path) or ".." in path:
            raise TelegramError("bad_file_path")
        try:
            resp = await self._egress.request(
                Service.TELEGRAM,
                "GET",
                f"/file{self._prefix()}/{path}",
                max_bytes=max_bytes,
            )
        except EgressResponseTooLarge:
            raise FileTooLarge("file_too_large") from None
        if not resp.is_success:
            raise TelegramError("download_failed", code=resp.status_code)
        if len(resp.content) > max_bytes:
            raise FileTooLarge("file_too_large")
        return resp.content
