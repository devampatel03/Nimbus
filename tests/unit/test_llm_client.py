# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""this session: `VLLMClient`'s complete/stream/structured wiring, against a
mocked underlying `openai.AsyncOpenAI` client -- no real vLLM server is
required (or available in this dev environment; see MEMORY.md). Each test
constructs a real `VLLMClient()` (cheap, no I/O per its own docstring) and
replaces `_client.chat.completions.create` with a fake, asserting the
request shape sent and the response parsing on the way back.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from src.llm.client import VLLMClient


def _fake_completion(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class _FakeStream:
    def __init__(self, deltas: list[str | None]) -> None:
        self._deltas = deltas

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> SimpleNamespace:
        if not self._deltas:
            raise StopAsyncIteration
        delta = self._deltas.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=delta))])


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int
    label: str


def test_base_url_and_model_come_from_settings_by_default() -> None:
    client = VLLMClient()
    assert client._model == "Qwen/Qwen2.5-7B-Instruct"
    assert str(client._client.base_url).startswith("http://localhost:8000")


@pytest.mark.asyncio
async def test_complete_returns_message_content() -> None:
    client = VLLMClient()
    client._client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
        return_value=_fake_completion("hi there")
    )

    result = await client.complete([{"role": "user", "content": "hello"}])

    assert result == "hi there"
    call_kwargs = client._client.chat.completions.create.call_args.kwargs
    assert call_kwargs["messages"] == [{"role": "user", "content": "hello"}]
    assert call_kwargs["model"] == client._model
    assert "stream" not in call_kwargs


@pytest.mark.asyncio
async def test_complete_handles_none_content() -> None:
    client = VLLMClient()
    client._client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
        return_value=_fake_completion(None)
    )

    result = await client.complete([{"role": "user", "content": "hello"}])

    assert result == ""


@pytest.mark.asyncio
async def test_stream_yields_non_empty_deltas_in_order() -> None:
    client = VLLMClient()
    client._client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
        return_value=_FakeStream(["hel", None, "lo", ""])
    )

    chunks = [chunk async for chunk in client.stream([{"role": "user", "content": "hi"}])]

    assert chunks == ["hel", "lo"]
    call_kwargs = client._client.chat.completions.create.call_args.kwargs
    assert call_kwargs["stream"] is True


@pytest.mark.asyncio
async def test_structured_parses_response_into_schema() -> None:
    client = VLLMClient()
    client._client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
        return_value=_fake_completion('{"x": 1, "label": "ok"}')
    )

    result = await client.structured([{"role": "user", "content": "give me json"}], schema=_Schema)

    assert result == _Schema(x=1, label="ok")
    call_kwargs = client._client.chat.completions.create.call_args.kwargs
    assert call_kwargs["response_format"] == {"type": "json_object"}
    assert call_kwargs["extra_body"] == {"guided_json": _Schema.model_json_schema()}


@pytest.mark.asyncio
async def test_structured_raises_on_schema_mismatch() -> None:
    client = VLLMClient()
    client._client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
        return_value=_fake_completion('{"x": "not-an-int"}')
    )

    with pytest.raises(ValidationError):
        await client.structured([{"role": "user", "content": "give me json"}], schema=_Schema)
