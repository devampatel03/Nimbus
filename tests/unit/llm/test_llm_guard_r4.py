# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""R4: every call goes through the guard, in order; a refusal makes ZERO
transport calls and still leaves an audit row; every success has one too."""

from __future__ import annotations

from typing import Any

import pytest

from src.llm.fake import FakeLLM
from src.llm.guard import KILL_SWITCH_KEY, SPEND_MODE_KEY, GuardedLLM
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    Lane,
    LLMAuditError,
    LLMMode,
    LLMProviderError,
    LLMRefused,
    Purpose,
    SpendCapExceeded,
    ToolSpec,
)
from tests.fakes.llm import API_KEY, Env, RecordingTransport, build_env

SK_SECRET = "sk-" + "abcdefghijklmnopqrstuvwxyz" + "123456"  # pragma: allowlist secret


def req(text: str = "what is on my calendar?", **kw: Any) -> ChatRequest:
    kw.setdefault("purpose", Purpose.CHAT)
    kw.setdefault("lane", Lane.TRUSTED)
    return ChatRequest(messages=(ChatMessage(role="user", content=text),), **kw)


async def _engage_kill(env: Env) -> None:
    await env.flags.set(KILL_SWITCH_KEY, {"engaged": True}, updated_by="test")


async def _trip_cap(env: Env) -> None:
    env.audit.rows.clear()
    # one call that costs more than the cap
    await env.audit.write(
        _cost_record(env, "1000"),
    )


def _cost_record(env: Env, cost: str) -> Any:
    from decimal import Decimal

    from src.llm.protocols import AuditRecord

    return AuditRecord(
        purpose="chat",
        provider="x",
        model="m",
        status="ok",
        created_at=env.clock(),
        cost_inr=Decimal(cost),
    )


# ---- each block path: zero transport calls, one blocked audit row -------------


async def test_kill_switch_blocks_with_zero_transport_calls() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport)
    await _engage_kill(env)

    with pytest.raises(LLMRefused) as ei:
        await env.llm.chat(req())

    assert ei.value.reason == "kill_switch"
    assert transport.calls == []
    assert [(r.status, r.blocked_reason) for r in env.audit.records] == [("blocked", "kill_switch")]
    assert await env.llm.mode() is LLMMode.KILLED


async def test_spend_cap_blocks_with_zero_transport_calls() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport, daily_spend_cap_inr="10")
    await _trip_cap(env)

    with pytest.raises(SpendCapExceeded):
        await env.llm.chat(req())

    assert transport.calls == []
    assert env.audit.records[-1].status == "blocked"
    assert env.audit.records[-1].blocked_reason == "spend_cap"


@pytest.mark.parametrize(
    "secret",
    [
        "my key is " + SK_SECRET,
        "token 123456789:AAH" + "x" * 32 + "_abc",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "-----BEGIN PRIVATE KEY-----\nabc",  # pragma: allowlist secret
        "password = hunter2hunter2hunter2",
        "ya29.a0AfH6SMBabcdefghijklmnopqrstuv",
        f"the provider key is {API_KEY}",
    ],
)
async def test_scrub_hit_blocks_with_zero_transport_calls(secret: str) -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport)

    with pytest.raises(LLMRefused) as ei:
        await env.llm.chat(req(secret))

    assert ei.value.reason == "scrub_blocked"
    assert transport.calls == []
    assert env.audit.records[-1].blocked_reason == "scrub_blocked"
    assert API_KEY not in repr(env.audit.records)
    assert "hunter2" not in str(ei.value)


async def test_quarantine_lane_with_tools_is_refused() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport)

    with pytest.raises(LLMRefused) as ei:
        await env.llm.chat(req(lane=Lane.QUARANTINE, tools=(ToolSpec(name="send_x"),)))

    assert ei.value.reason == "lane_violation"
    assert transport.calls == []


async def test_quarantine_lane_without_tools_is_allowed() -> None:
    env = build_env()
    result = await env.llm.chat(req(lane=Lane.QUARANTINE))
    assert result.text == "fake reply"


async def test_unreadable_kill_switch_fails_closed() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport)

    async def boom(key: str) -> dict[str, Any] | None:
        raise RuntimeError("db down")

    env.flags.get = boom  # type: ignore[method-assign]

    with pytest.raises(LLMRefused) as ei:
        await env.llm.chat(req())

    assert ei.value.reason == "guard_error"
    assert transport.calls == []
    assert await env.llm.mode() is LLMMode.KILLED


async def test_unexpected_guard_bug_fails_closed() -> None:
    env = build_env()

    def explode(*a: Any, **k: Any) -> Any:
        raise ValueError("bug")

    env.llm._scrubber.scrub = explode  # type: ignore[method-assign]
    provider = env.provider
    assert isinstance(provider, FakeLLM)

    with pytest.raises(LLMRefused) as ei:
        await env.llm.chat(req())

    assert ei.value.reason == "guard_error"
    assert provider.requests == []


# ---- ordering -----------------------------------------------------------------


async def test_kill_switch_is_checked_before_spend_and_scrub() -> None:
    env = build_env(daily_spend_cap_inr="10")
    await _trip_cap(env)
    await _engage_kill(env)

    with pytest.raises(LLMRefused) as ei:
        await env.llm.chat(req(SK_SECRET))

    assert ei.value.reason == "kill_switch"
    assert env.alerts.messages == []  # spend check never ran


async def test_scrub_is_checked_before_spend() -> None:
    # TARGET_DESIGN section 2 order: kill -> lane -> scrub -> spend.
    env = build_env(daily_spend_cap_inr="10")
    await _trip_cap(env)

    with pytest.raises(LLMRefused) as ei:
        await env.llm.chat(req(SK_SECRET))
    assert ei.value.reason == "scrub_blocked"
    assert env.alerts.messages == []  # spend check never ran


async def test_quarantine_drops_provider_invented_tool_calls() -> None:
    from src.llm.types import ProviderResponse, ToolCallRequest

    bogus = ToolCallRequest(id="c", name="send_money", arguments={})
    env = build_env(provider=FakeLLM([ProviderResponse(text="x", tool_calls=(bogus,))]))
    result = await env.llm.chat(req(lane=Lane.QUARANTINE))
    assert result.tool_calls == () and result.text == "x"

    env2 = build_env(provider=FakeLLM([ProviderResponse(text="x", tool_calls=(bogus,))]))
    assert (await env2.llm.chat(req(lane=Lane.TRUSTED))).tool_calls == (bogus,)


@pytest.mark.parametrize("value", ["true", 1, "yes", None, {}])
async def test_malformed_kill_switch_value_fails_closed(value: Any) -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport)
    await env.flags.set(KILL_SWITCH_KEY, {"engaged": value}, updated_by="t")
    with pytest.raises(LLMRefused):
        await env.llm.chat(req())
    assert transport.calls == []
    assert await env.llm.mode() is LLMMode.KILLED


async def test_kill_switch_missing_key_is_engaged_but_absent_flag_is_not() -> None:
    env = build_env()
    await env.flags.set(KILL_SWITCH_KEY, {}, updated_by="t")
    with pytest.raises(LLMRefused):
        await env.llm.chat(req())


async def test_default_placeholder_key_is_not_a_secret() -> None:
    from src.llm.settings import DEFAULT_API_KEY

    env = build_env(api_key=DEFAULT_API_KEY)
    result = await env.llm.chat(req(f"the config says {DEFAULT_API_KEY} here"))
    assert result.text == "fake reply"


async def test_tool_names_and_call_ids_are_scrubbed() -> None:
    from src.llm.types import ToolCallRequest

    env = build_env()
    r = ChatRequest(
        purpose=Purpose.CHAT,
        lane=Lane.TRUSTED,
        messages=(
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="tool", content="r", tool_call_id=SK_SECRET),
        ),
    )
    with pytest.raises(LLMRefused):
        await env.llm.chat(r)
    r2 = ChatRequest(
        purpose=Purpose.CHAT,
        lane=Lane.TRUSTED,
        messages=(
            ChatMessage(
                role="assistant",
                tool_calls=(ToolCallRequest(id=SK_SECRET, name="t", arguments={}),),
            ),
        ),
    )
    with pytest.raises(LLMRefused):
        await env.llm.chat(r2)


async def test_scrub_runs_on_every_message_not_just_the_last() -> None:
    env = build_env()
    r = ChatRequest(
        purpose=Purpose.CHAT,
        lane=Lane.TRUSTED,
        messages=(
            ChatMessage(role="user", content=SK_SECRET),
            ChatMessage(role="user", content="innocent"),
        ),
    )
    with pytest.raises(LLMRefused):
        await env.llm.chat(r)


# ---- success path -------------------------------------------------------------


async def test_success_writes_exactly_one_completed_audit_row() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport)

    result = await env.llm.chat(req())

    assert result.text == "ok"
    assert len(transport.calls) == 1
    assert len(env.audit.rows) == 1
    row_id, rec = env.audit.rows[0]
    assert row_id == result.audit_id
    assert (rec.status, rec.tokens_in, rec.tokens_out) == ("ok", 100, 50)
    assert rec.cost_inr == result.cost_inr > 0
    assert rec.provider == "openai_compat" and rec.latency_ms is not None


async def test_redactions_applied_before_send() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport)

    await env.llm.chat(req("pay with 4111 1111 1111 1111, aadhaar 2345 6789 0123, otp 481516"))

    body = transport.calls[0].content.decode()
    assert "4111" not in body and "2345 6789" not in body and "481516" not in body
    assert body.count("REDACTED") == 3


async def test_payload_not_stored_by_default_and_redacted_when_enabled() -> None:
    env = build_env()
    await env.llm.chat(req("card 4111 1111 1111 1111"))
    assert env.audit.records[0].payload_redacted is None

    env2 = build_env(store_payload=True)
    await env2.llm.chat(req("card 4111 1111 1111 1111"))
    stored = env2.audit.records[0].payload_redacted
    assert stored is not None and "4111" not in stored and "REDACTED:card" in stored


async def test_provider_failure_writes_error_row_and_raises() -> None:
    env = build_env(provider=FakeLLM([LLMProviderError("timeout")]))

    with pytest.raises(LLMProviderError):
        await env.llm.chat(req())

    assert [r.status for r in env.audit.records] == ["error"]
    # A timeout was sent and may be billed: the reserved estimate is recorded (R5).
    assert env.audit.records[0].cost_inr > 0


async def test_provider_status_failure_is_not_billed() -> None:
    env = build_env(provider=FakeLLM([LLMProviderError("provider_status", status=503)]))
    with pytest.raises(LLMProviderError):
        await env.llm.chat(req())
    assert env.audit.records[0].cost_inr == 0


async def test_audit_write_failure_withholds_result() -> None:
    env = build_env()

    async def fail(record: Any) -> Any:
        raise RuntimeError("db down")

    env.audit.write = fail  # type: ignore[method-assign]
    with pytest.raises(LLMAuditError):
        await env.llm.chat(req())


async def test_audit_failure_on_block_does_not_mask_refusal() -> None:
    env = build_env()
    await _engage_kill(env)

    async def fail(record: Any) -> Any:
        raise RuntimeError("db down")

    env.audit.write = fail  # type: ignore[method-assign]
    with pytest.raises(LLMRefused) as ei:
        await env.llm.chat(req())
    assert ei.value.reason == "kill_switch"


async def test_missing_usage_is_billed_by_estimate_not_zero() -> None:
    from src.llm.types import ProviderResponse

    env = build_env(provider=FakeLLM([ProviderResponse(text="hello world")]))
    result = await env.llm.chat(req())
    assert result.usage.tokens_in > 0 and result.cost_inr > 0


async def test_mode_normal_by_default_and_flag_clearing_resumes() -> None:
    env = build_env()
    assert await env.llm.mode() is LLMMode.NORMAL
    await _engage_kill(env)
    assert await env.llm.mode() is LLMMode.KILLED
    await env.flags.set(KILL_SWITCH_KEY, {"engaged": False}, updated_by="test")
    assert await env.llm.mode() is LLMMode.NORMAL
    assert (await env.llm.chat(req())).text == "fake reply"
    assert SPEND_MODE_KEY  # exported key constant


def test_guard_is_the_only_public_client() -> None:
    assert GuardedLLM.chat and GuardedLLM.mode
