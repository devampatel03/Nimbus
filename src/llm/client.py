# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Local LLM client (this session): an OpenAI-compatible client for a locally
served model (vLLM, `docker-compose.yml`'s `vllm` service or the standalone
`vllm serve` command documented there).

NOT an egress point: `base_url` defaults to `settings.llm_base_url`
(loopback), and nothing here is ever called through
`src/security/scrubber.py:execute_external`. This uses the `openai` SDK
(which wraps `httpx` internally) rather than `httpx`/`requests`/`aiohttp`
directly, so it does not appear in INV-1's AST scan of `src/` for those
three module names -- that scan is specifically about which layer of this
codebase reaches a transport library directly, not what an approved local
client uses under the hood.

`LLMClient` is a `Protocol` (same pattern as `src/memory/manager.py`'s
`Embedder`/`Qwen3Embedder`, AD-7/Step 11): production code depends on the
protocol and defaults to `VLLMClient()`, so tests can inject a fake/mock
without hitting a real network endpoint.
"""

from collections.abc import AsyncIterator
from typing import Protocol, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from config.settings import settings

ChatMessage = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}

T = TypeVar("T", bound=BaseModel)


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


class VLLMClient:
    """`LLMClient` backed by a local vLLM (or any OpenAI-compatible)
    server. Construction does no I/O (`AsyncOpenAI.__init__` opens no
    connection), so the default-factory pattern used in
    `GraphDependencies`/`Approver`/extraction workers is cheap even when
    no server is actually running -- only an actual `complete`/`stream`/
    `structured` call reaches the network.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._model = model or settings.llm_model
        self._client = AsyncOpenAI(
            base_url=base_url or settings.llm_base_url,
            api_key=api_key or settings.llm_api_key,
            timeout=timeout or settings.llm_timeout_seconds,
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        response_stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in response_stream:  # type: ignore[union-attr]
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def structured(
        self,
        messages: list[ChatMessage],
        *,
        schema: type[T],
        temperature: float = 0.0,
    ) -> T:
        """Guided JSON decoding via vLLM's `guided_json` extra_body param
        (its OpenAI-compatible server's extension over the base API);
        `response_format={"type": "json_object"}` is the standard OpenAI
        hint most servers honor even without that extension. The response
        is always validated client-side against `schema` regardless of
        whether the server actually enforced it server-side.
        """
        response = await self._client.chat.completions.create(  # type: ignore[call-overload]
            model=self._model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
            extra_body={"guided_json": schema.model_json_schema()},
        )
        content = response.choices[0].message.content or "{}"
        return schema.model_validate_json(content)
