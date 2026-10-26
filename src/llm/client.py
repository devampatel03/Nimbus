# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Back-compat shim (P1.4b, TARGET_DESIGN M8). DEPRECATED.

`LLMClient` / `VLLMClient` / `ChatMessage` here are the pre-pivot
complete / stream / structured API, still imported by
`src/memory/extractor.py` and `src/personality`. They are now a
thin adapter over `GuardedLLM`, so every legacy call also passes the guard
(kill switch, spend cap, scrub, audit) and egresses only via `src.net`.
New code uses `src.llm.protocols.LLMClient` / `src.llm.types.ChatRequest`.
P2.5b / P2.x remove the legacy callers; this file is deleted with them.

Behavioural notes vs. the old client: the target is a cloud HTTPS provider
(the loopback vLLM URL is refused by the egress allowlist); `stream()` yields
the whole reply as one chunk (the pivot never streams); `structured()` raises
`LLMResponseError` instead of pydantic's `ValidationError` on a bad reply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, TypeVar

from pydantic import BaseModel

from src.llm.factory import build_guarded_llm
from src.llm.guard import GuardedLLM
from src.llm.settings import LLMSettings
from src.llm.types import ChatMessage as _Message
from src.llm.types import ChatRequest, Lane, LLMResponseError, Purpose

ChatMessage = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}

T = TypeVar("T", bound=BaseModel)

_DEFAULT_MAX_TOKENS = 800


class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str: ...

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...

    async def structured(
        self,
        messages: list[ChatMessage],
        *,
        schema: type[T],
        temperature: float = 0.0,
    ) -> T: ...


def _convert(messages: list[ChatMessage]) -> tuple[_Message, ...]:
    return tuple(_Message(role=m["role"], content=m["content"]) for m in messages)  # type: ignore[arg-type]


class VLLMClient:
    """Legacy-API client over `GuardedLLM`. Construction does no I/O."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        guarded: GuardedLLM | None = None,
    ) -> None:
        if guarded is not None:
            self._llm = guarded
            return
        overrides: dict[str, object] = {}
        if base_url:
            overrides["base_url"] = base_url
        if api_key:
            overrides["api_key"] = api_key
        if model:
            overrides["model"] = model
        if timeout:
            overrides["timeout_seconds"] = timeout
        self._llm = build_guarded_llm(LLMSettings(**overrides))  # type: ignore[arg-type]

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        result = await self._llm.chat(
            ChatRequest(
                purpose=Purpose.CHAT,
                lane=Lane.SYSTEM,
                messages=_convert(messages),
                temperature=temperature,
                max_output_tokens=max_tokens or _DEFAULT_MAX_TOKENS,
            )
        )
        return result.text or ""

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        text = await self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        if text:
            yield text

    async def structured(
        self,
        messages: list[ChatMessage],
        *,
        schema: type[T],
        temperature: float = 0.0,
    ) -> T:
        result = await self._llm.chat(
            ChatRequest(
                purpose=Purpose.CHAT,
                lane=Lane.SYSTEM,
                messages=_convert(messages),
                response_schema=schema,
                temperature=temperature,
            )
        )
        if not isinstance(result.parsed, schema):
            raise LLMResponseError("schema_mismatch")
        return result.parsed
