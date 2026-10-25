# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""OpenAI-compatible adapter: wire shape, tool calling, structured output,
bounded retries, egress denial logging, INV-7 (key never leaks)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from src.llm.adapter import OpenAICompatProvider
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    Lane,
    LLMProviderError,
    LLMResponseError,
    Purpose,
    ToolCallRequest,
    ToolSpec,
)
from src.net import NetSettings
from tests.fakes.llm import (
    API_KEY,
    LLM_URL,
    RecordingTransport,
    build_env,
    completion_response,
    llm_settings,
    no_sleep,
)


def req(text: str = "hello", **kw: object) -> ChatRequest:
    return ChatRequest(
        purpose=Purpose.CHAT,
        lane=Lane.TRUSTED,
        messages=(ChatMessage(role="user", content=text),),
        **kw,  # type: ignore[arg-type]
    )


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int


def err(status: int, body: str = "{}", **headers: str) -> httpx.Response:
    return httpx.Response(status, content=body.encode(), headers=headers)


def sequence(*responses: httpx.Response) -> RecordingTransport:
    items = list(responses)
    return RecordingTransport(lambda r: items.pop(0))


async def test_request_shape_and_auth_header() -> None:
    t = RecordingTransport()
    env = build_env(transport=t)
    await env.llm.chat(req(temperature=0.1, max_output_tokens=33))

    call = t.calls[0]
    assert call.url.host == "llm.example.com" and call.url.path.endswith("/chat/completions")
    assert call.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(call.content)
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["temperature"] == 0.1 and body["max_tokens"] == 33
    assert "tools" not in body


async def test_tool_calling_messages_and_response_parsing() -> None:
    tool_call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "create_task", "arguments": '{"title": "milk"}'},
    }
    t = sequence(completion_response(None, tool_calls=[tool_call]))
    env = build_env(transport=t)
    r = ChatRequest(
        purpose=Purpose.CHAT,
        lane=Lane.TRUSTED,
        messages=(
            ChatMessage(role="user", content="add milk"),
            ChatMessage(
                role="assistant",
                tool_calls=(ToolCallRequest(id="p0", name="lookup", arguments={"q": "milk"}),),
            ),
            ChatMessage(role="tool", content="found", tool_call_id="p0"),
        ),
        tools=(
            ToolSpec(
                name="create_task",
                description="d",
                parameters={"type": "object", "properties": {"title": {"type": "string"}}},
            ),
        ),
    )

    result = await env.llm.chat(r)

    assert result.text is None
    assert result.tool_calls == (
        ToolCallRequest(id="c1", name="create_task", arguments={"title": "milk"}),
    )
    body = json.loads(t.calls[0].content)
    assert body["tools"][0]["function"]["name"] == "create_task"
    assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"q": "milk"}'
    assert body["messages"][2] == {"role": "tool", "content": "found", "tool_call_id": "p0"}


@pytest.mark.parametrize("args", ["not json", "[1, 2]"])
async def test_bad_tool_arguments_rejected(args: str) -> None:
    bad = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": args}}
    env = build_env(transport=sequence(completion_response(None, tool_calls=[bad])))
    with pytest.raises(LLMResponseError):
        await env.llm.chat(req())


async def test_structured_output_validates_client_side() -> None:
    t = sequence(completion_response('{"x": 4}'))
    env = build_env(transport=t)
    result = await env.llm.chat(req(response_schema=Answer))
    assert result.parsed == Answer(x=4)
    body = json.loads(t.calls[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in body["messages"][0]["content"]


async def test_structured_output_accepts_code_fence() -> None:
    env = build_env(transport=sequence(completion_response('```json\n{"x": 1}\n```')))
    assert (await env.llm.chat(req(response_schema=Answer))).parsed == Answer(x=1)


async def test_structured_repair_retry_is_a_second_audited_call() -> None:
    t = sequence(completion_response("sure! x is 3"), completion_response('{"x": 3}'))
    env = build_env(transport=t)

    result = await env.llm.chat(req(response_schema=Answer))

    assert result.parsed == Answer(x=3)
    assert len(t.calls) == 2 and len(env.audit.rows) == 2
    repair_body = json.loads(t.calls[1].content)
    assert repair_body["messages"][-2] == {"role": "assistant", "content": "sure! x is 3"}


async def test_structured_gives_up_after_one_repair() -> None:
    t = sequence(completion_response("nope"), completion_response("still nope"))
    env = build_env(transport=t)
    with pytest.raises(LLMResponseError):
        await env.llm.chat(req(response_schema=Answer))
    assert len(t.calls) == 2


# ---- retries ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_retries_transient_status_then_succeeds(status: int) -> None:
    t = sequence(err(status), err(status), completion_response("fine"))
    env = build_env(transport=t)
    assert (await env.llm.chat(req())).text == "fine"
    assert len(t.calls) == 3
    assert len(env.audit.rows) == 1  # retries live inside ONE audited call


async def test_retry_is_bounded_and_audits_error() -> None:
    t = RecordingTransport(lambda r: err(503))
    env = build_env(transport=t, max_retries=2)
    with pytest.raises(LLMProviderError) as ei:
        await env.llm.chat(req())
    assert len(t.calls) == 3 and ei.value.status == 503
    assert [r.status for r in env.audit.records] == ["error"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_never_retries_4xx(status: int) -> None:
    t = RecordingTransport(lambda r: err(status))
    env = build_env(transport=t)
    with pytest.raises(LLMProviderError) as ei:
        await env.llm.chat(req())
    assert len(t.calls) == 1 and ei.value.status == status


async def test_timeouts_are_not_retried_in_adapter() -> None:
    # The request was sent and may be billed; a retry here would bypass the
    # guard's per-call reservation and audit row (R5). Callers retry via guard.
    t = RecordingTransport(_raise_timeout)
    env = build_env(transport=t)
    with pytest.raises(LLMProviderError) as ei:
        await env.llm.chat(req())
    assert ei.value.code == "timeout" and len(t.calls) == 1


def _raise_timeout(r: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=r)


async def test_retry_after_header_is_honoured_but_capped() -> None:
    sleeps: list[float] = []

    async def spy_sleep(s: float) -> None:
        sleeps.append(s)

    t = sequence(
        err(429, **{"retry-after": "7"}), err(429, **{"retry-after": "9999"}), completion_response()
    )
    settings = llm_settings()
    p = OpenAICompatProvider(
        settings, net_settings=NetSettings(llm_base_url=LLM_URL), transport=t, sleep=spy_sleep
    )
    await p.complete(req())
    assert sleeps[0] >= 7 and sleeps[1] <= 30.0


def test_backoff_grows_exponentially_and_is_capped() -> None:
    p = OpenAICompatProvider(llm_settings(), sleep=no_sleep, jitter=lambda: 1.0)
    delays = [p._backoff(i, None) for i in range(6)]
    assert delays[0] == 0.5 and delays[1] == 1.0 and delays[2] == 2.0
    assert max(delays) == 8.0


# ---- egress ----------------------------------------------------------------------


async def test_egress_denial_is_logged_sanitised_and_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = RecordingTransport()
    # Net allowlist resolves the LLM service to a different host than base_url.
    p = OpenAICompatProvider(
        llm_settings(base_url="https://other.example.org/v1"),
        net_settings=NetSettings(llm_base_url=LLM_URL),
        transport=t,
        sleep=no_sleep,
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LLMProviderError) as ei:
        await p.complete(req("secret-ish prompt body"))

    assert ei.value.code == "egress_denied"
    assert t.calls == []
    lines = [r.getMessage() for r in caplog.records if "egress denied" in r.getMessage()]
    assert lines == ["egress denied service=None method=POST reason=host_not_allowlisted"]
    assert "secret-ish" not in caplog.text and "other.example.org" not in caplog.text


async def test_non_https_base_url_is_refused_without_io() -> None:
    p = OpenAICompatProvider(llm_settings(base_url="http://localhost:8000/v1"), sleep=no_sleep)
    with pytest.raises(LLMProviderError) as ei:
        await p.complete(req())
    assert ei.value.code == "egress_config"


async def test_provider_construction_does_no_io_and_close_is_idempotent() -> None:
    p = OpenAICompatProvider(llm_settings(base_url="http://localhost:8000/v1"))
    await p.aclose()
    t = RecordingTransport()
    p2 = OpenAICompatProvider(
        llm_settings(), transport=t, net_settings=NetSettings(llm_base_url=LLM_URL)
    )
    await p2.complete(req())
    await p2.aclose()
    await p2.aclose()


# ---- INV-7 -----------------------------------------------------------------------


async def test_api_key_never_in_logs_exceptions_or_audit(caplog: pytest.LogCaptureFixture) -> None:
    echo = json.dumps({"error": {"message": f"Incorrect API key provided: {API_KEY}"}})
    t = RecordingTransport(lambda r: err(401, echo))
    env = build_env(transport=t, store_payload=True)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LLMProviderError) as ei:
        await env.llm.chat(req("hello there"))

    blob = " ".join(
        [
            str(ei.value),
            repr(ei.value),
            caplog.text,
            repr(env.audit.records),
            repr(env.llm._settings),
            repr(env.provider.__dict__),
        ]
    )
    assert API_KEY not in blob
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    # and the key did reach the provider exactly where it belongs
    assert t.calls[0].headers["authorization"] == f"Bearer {API_KEY}"


async def test_success_logs_carry_no_prompt_or_key(caplog: pytest.LogCaptureFixture) -> None:
    env = build_env(transport=RecordingTransport())
    caplog.set_level(logging.DEBUG)
    await env.llm.chat(req("my very private sentence"))
    assert "private sentence" not in caplog.text and API_KEY not in caplog.text
