# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Back-compat `VLLMClient` shim (was tests/unit/test_llm_client.py): the
legacy complete / stream / structured API now runs over `GuardedLLM`."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from src.llm.client import VLLMClient
from src.llm.types import LLMProviderError, LLMRefused, LLMResponseError
from tests.fakes.llm import RecordingTransport, build_env, completion_response


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int
    label: str


def shim(*responses: object) -> tuple[VLLMClient, RecordingTransport]:
    items = list(responses)
    t = RecordingTransport(lambda r: items.pop(0))  # type: ignore[arg-type,return-value]
    env = build_env(transport=t)
    return VLLMClient(guarded=env.llm), t


async def test_complete_returns_text_and_sends_messages() -> None:
    c, t = shim(completion_response("hi there"))
    assert (
        await c.complete([{"role": "user", "content": "hello"}], temperature=0.5, max_tokens=9)
        == "hi there"
    )
    body = json.loads(t.calls[0].content)
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["temperature"] == 0.5 and body["max_tokens"] == 9


async def test_complete_empty_content_is_empty_string() -> None:
    c, _ = shim(completion_response(None))
    assert await c.complete([{"role": "user", "content": "x"}]) == ""


async def test_stream_yields_whole_reply_once() -> None:
    c, _ = shim(completion_response("alpha beta"))
    chunks = [d async for d in c.stream([{"role": "user", "content": "x"}])]
    assert chunks == ["alpha beta"]


async def test_stream_empty_reply_yields_nothing() -> None:
    c, _ = shim(completion_response(""))
    assert [d async for d in c.stream([{"role": "user", "content": "x"}])] == []


async def test_structured_validates() -> None:
    c, _ = shim(completion_response('{"x": 7, "label": "ok"}'))
    out = await c.structured([{"role": "user", "content": "x"}], schema=_Schema)
    assert out == _Schema(x=7, label="ok")


async def test_structured_rejects_bad_reply_after_repair() -> None:
    c, _ = shim(completion_response('{"x": "no"}'), completion_response('{"x": "no"}'))
    with pytest.raises(LLMResponseError):
        await c.structured([{"role": "user", "content": "x"}], schema=_Schema)


async def test_legacy_calls_pass_the_guard() -> None:
    t = RecordingTransport()
    env = build_env(transport=t)
    from src.llm.guard import KILL_SWITCH_KEY

    await env.flags.set(KILL_SWITCH_KEY, {"engaged": True}, updated_by="t")
    c = VLLMClient(guarded=env.llm)
    with pytest.raises(LLMRefused):
        await c.complete([{"role": "user", "content": "x"}])
    assert t.calls == []


async def test_default_construction_is_cheap_and_loopback_url_fails_closed() -> None:
    c = VLLMClient(base_url="http://localhost:8000/v1", api_key="k", model="m", timeout=5)
    with pytest.raises(LLMProviderError) as ei:
        await c.complete([{"role": "user", "content": "x"}])
    assert ei.value.code == "egress_config"
    VLLMClient()  # no args: no I/O, no exception
