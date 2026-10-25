# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""OpenAI-compatible `Provider` (ADR-0003/0004). The ONLY module that imports
an LLM SDK (`openai`); the import-lint test enforces that.

Egress: the SDK's HTTP client is `src.net.build_sdk_http_client(Service.LLM)`,
so every request, including retries, passes the host allowlist and deny rules.
The SDK's own retries are disabled (`max_retries=0`); this module retries
429 / 5xx / timeouts / connection errors with bounded exponential backoff and
NEVER retries 4xx (auth, validation) or an egress denial.

INV-7: SDK exception text can echo the key prefix ("Incorrect API key ...")
or a response body, so provider failures are re-raised as fixed-code
`LLMProviderError`s with `from None`. Nothing here logs prompt text or headers.
The SDK client is built lazily so constructing a provider does no I/O and a
bad base URL fails at first use (closed), not at import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import openai
from openai import AsyncOpenAI
from pydantic import ValidationError

from src.llm.settings import LLMSettings
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    LLMProviderError,
    LLMResponseError,
    ProviderResponse,
    ToolCallRequest,
    Usage,
)
from src.net import (
    EgressConfigError,
    EgressDenied,
    EgressError,
    NetSettings,
    Service,
    build_sdk_http_client,
)

logger = logging.getLogger("nimbus.llm")

_MAX_RETRY_AFTER_S = 30.0
Sleep = Callable[[float], Awaitable[None]]


def _egress_error(exc: BaseException) -> EgressError | None:
    """Find a `src.net` error in the cause/context chain (the SDK wraps what
    the transport raised in `APIConnectionError`)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, EgressError):
            return cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return None


def _to_wire_message(m: ChatMessage) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        msg["tool_call_id"] = m.tool_call_id
    return msg


def build_wire_request(req: ChatRequest, model: str) -> dict[str, Any]:
    messages = [_to_wire_message(m) for m in req.messages]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": req.temperature,
        "max_tokens": req.max_output_tokens,
    }
    if req.tools:
        kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in req.tools
        ]
    if req.response_schema is not None:
        schema = json.dumps(req.response_schema.model_json_schema(), sort_keys=True)
        kwargs["response_format"] = {"type": "json_object"}
        messages.insert(
            0,
            {
                "role": "system",
                "content": f"Reply with a single JSON object matching this JSON Schema: {schema}",
            },
        )
    return kwargs


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        settings: LLMSettings | None = None,
        *,
        net_settings: NetSettings | None = None,
        transport: Any = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings if settings is not None else LLMSettings()
        self._net_settings = net_settings
        self._transport = transport
        self._sleep = sleep
        self._jitter = jitter
        self._client: AsyncOpenAI | None = None
        self.model = self._settings.model

    def _sdk(self) -> AsyncOpenAI:
        if self._client is None:
            try:
                net = self._net_settings or NetSettings(llm_base_url=self._settings.base_url)
                http_client = build_sdk_http_client(Service.LLM, net, transport=self._transport)
            except (EgressConfigError, ValidationError):
                raise LLMProviderError("egress_config") from None
            self._client = AsyncOpenAI(
                base_url=self._settings.base_url,
                api_key=self._settings.api_key.get_secret_value(),
                # openai>=3 types http_client as httpx2 but accepts a legacy httpx client.
                http_client=http_client,  # type: ignore[arg-type]
                max_retries=0,
                timeout=self._settings.timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def complete(self, req: ChatRequest) -> ProviderResponse:
        kwargs = build_wire_request(req, self.model)
        attempts = self._settings.max_retries + 1
        for attempt in range(attempts):
            try:
                raw = await self._sdk().chat.completions.create(**kwargs)
            except LLMProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below, never re-raised raw
                delay_hint = self._classify(exc, attempt, attempts)
                await self._sleep(self._backoff(attempt, delay_hint))
                continue
            return self._parse(raw)
        raise LLMProviderError("retries_exhausted")  # pragma: no cover

    # ------------------------------------------------------------- helpers

    def _classify(self, exc: Exception, attempt: int, attempts: int) -> float | None:
        """Return a Retry-After hint (or 0.0) if the call should be retried;
        raise a sanitised `LLMProviderError` otherwise."""
        egress = _egress_error(exc)
        if egress is not None:
            logger.warning(
                "egress %s service=%s method=%s reason=%s",
                "denied" if isinstance(egress, EgressDenied) else "failed",
                egress.service,
                egress.method,
                egress.reason,
            )
            if isinstance(egress, EgressDenied):
                raise LLMProviderError("egress_denied") from None
            raise LLMProviderError("egress_failed") from None

        retryable = True
        status: int | None = None
        hint: float | None = None
        if isinstance(exc, openai.APIStatusError):
            status = exc.status_code
            retryable = status == 429 or status >= 500
            retry_after = exc.response.headers.get("retry-after")
            if retry_after is not None:
                try:
                    hint = min(max(float(retry_after), 0.0), _MAX_RETRY_AFTER_S)
                except ValueError:
                    hint = None
            code = "provider_status"
        elif isinstance(exc, openai.APITimeoutError):
            code = "timeout"
        elif isinstance(exc, openai.APIConnectionError):
            code = "connection"
        else:
            code = "unexpected"
            retryable = False

        if not retryable:
            raise LLMProviderError(code, status=status) from None
        if attempt + 1 >= attempts:
            raise LLMProviderError(code, status=status) from None
        logger.warning("llm provider retry code=%s status=%s attempt=%d", code, status, attempt + 1)
        return hint if hint is not None else 0.0

    def _backoff(self, attempt: int, hint: float | None) -> float:
        s = self._settings
        exp = min(s.backoff_max_s, s.backoff_base_s * (2**attempt))
        delay = exp * (0.5 + self._jitter() / 2)
        return float(min(max(delay, hint or 0.0), max(s.backoff_max_s, _MAX_RETRY_AFTER_S)))

    @staticmethod
    def _parse(raw: Any) -> ProviderResponse:
        usage = Usage()
        try:
            if raw.usage is not None:
                usage = Usage(
                    tokens_in=int(raw.usage.prompt_tokens or 0),
                    tokens_out=int(raw.usage.completion_tokens or 0),
                )
        except Exception:  # noqa: BLE001 - unusable usage block => estimate billing
            usage = Usage()
        try:
            choice = raw.choices[0]
            message = choice.message
            calls: list[ToolCallRequest] = []
            for tc in message.tool_calls or []:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except ValueError:
                    raise LLMResponseError("bad_tool_arguments", usage=usage) from None
                if not isinstance(args, dict):
                    raise LLMResponseError("bad_tool_arguments", usage=usage)
                calls.append(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=args))
            return ProviderResponse(text=message.content, tool_calls=tuple(calls), usage=usage)
        except LLMResponseError:
            raise
        except Exception:  # noqa: BLE001 - malformed provider payload
            raise LLMResponseError("malformed_response", usage=usage) from None
