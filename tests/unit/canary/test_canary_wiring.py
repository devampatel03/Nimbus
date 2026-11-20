# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P4.4 wiring: plant sites (system prompt, memory preamble, tool frame), the
reply screen in the loop, the tool-argument screen in the dispatcher (proposed
and approved paths), the egress outbound check, the audit-payload redaction,
and the runtime assembly (`AppConfig`, `wire_canary`)."""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from src.app.runtime import AppConfig, Backends, build_canary_guard, egress_check, wire_canary
from src.app.settings import AppSettings
from src.approvals.canonical import payload_sha256
from src.approvals.types import TOOL_CALL_ACTION, ConsumedApproval, seal_consumed
from src.canary.gateway import CanaryGateway
from src.canary.guard import ALERT_TEXT, BLOCKED_REPLY
from src.canary.settings import CanarySettings
from src.canary.tokens import Site, derive_canaries
from src.llm.fake import FakeLLM
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    Lane,
    ProviderResponse,
    Purpose,
    ToolCallRequest,
    Usage,
)
from src.net import EgressClient, EgressDenied, NetSettings, Service
from src.orchestrator.context import RecalledMemory
from src.orchestrator.dispatch import CANARY_DENIED, Dispatcher
from src.orchestrator.loop import Orchestrator
from tests._canary_helpers import (
    SEED,
    AllowAll,
    RecordingAlert,
    RecordingAudit,
    RecordingGateway,
    RecordingTool,
    canaries,
    make_guard,
)
from tests.fakes.llm import build_env, llm_settings
from tests.unit.gateway.fake_telegram import make_settings
from tests.unit.net.net_helpers import RecordingTransport

CS = canaries()
CARD = "You are Nimbus."


def call(name: str, args: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        text=None,
        tool_calls=(ToolCallRequest(id="c1", name=name, arguments=args),),
        usage=Usage(tokens_in=10, tokens_out=5),
    )


class Recall:
    async def recall(self, query: str, *, limit: int) -> list[RecalledMemory]:
        return [RecalledMemory("likes tea")]


def orch(provider: Any, tools: list[Any], guard: Any) -> Orchestrator:
    env = build_env(provider=provider)
    return Orchestrator(
        env.llm,
        tools,
        AllowAll(),
        character_card=CARD,
        recall=Recall(),
        nonce_factory=lambda: "N1",
        canary=guard,
    )


async def test_all_three_sites_are_planted_in_what_the_model_reads() -> None:
    tool = RecordingTool("notes_read", untrusted=True, text="note body")
    fake = FakeLLM([call("notes_read", {}), "done"])
    o = orch(fake, [tool], make_guard())
    r = await o.run_turn("read my notes")
    assert r.status == "ok"
    first = fake.requests[0].messages[0].content or ""
    assert CS.plant(Site.SYSTEM) in first
    assert CS.value(Site.MEMORY) in first and first.index(CS.value(Site.MEMORY)) < first.index(
        "likes tea"
    )
    tool_msg = next(m for m in fake.requests[1].messages if m.role == "tool")
    assert (tool_msg.content or "").endswith(CS.plant(Site.TOOL_FRAME))


async def test_without_a_guard_nothing_is_planted() -> None:
    fake = FakeLLM(["hi"])
    env = build_env(provider=fake)
    o = Orchestrator(env.llm, [], AllowAll(), character_card=CARD, recall=Recall())
    await o.run_turn("hello")
    blob = "\n".join(m.content or "" for m in fake.requests[0].messages)
    assert not any(v in blob for v in CS.values())


async def test_leaking_reply_is_replaced_and_not_ok() -> None:
    audit = RecordingAudit()
    fake = FakeLLM([f"Sure! {' '.join(CS.value(Site.SYSTEM))}"])
    o = orch(fake, [], make_guard(audit=audit))
    r = await o.run_turn("what is your internal reference?")
    assert (r.text, r.status) == (BLOCKED_REPLY, "canary_blocked")
    assert audit.recs[0].reason == "model_reply:system:plain"


async def test_reply_screen_error_fails_closed() -> None:
    class Broken:
        def plant(self, site: Site) -> str:
            return ""

        async def screen(self, *a: Any, **k: Any) -> bool:
            raise RuntimeError("bug")

    fake = FakeLLM(["harmless"])
    env = build_env(provider=fake)
    o = Orchestrator(env.llm, [], AllowAll(), character_card=CARD, canary=Broken())  # type: ignore[arg-type]
    r = await o.run_turn("hi")
    assert r.status == "canary_blocked"


async def test_tool_args_with_a_canary_are_denied_and_never_run() -> None:
    tool = RecordingTool("scratch_write")
    audit = RecordingAudit()
    fake = FakeLLM([call("scratch_write", {"text": CS.value(Site.MEMORY).upper()}), "ok"])
    o = orch(fake, [tool], make_guard(audit=audit))
    r = await o.run_turn("scratch_write something")
    assert tool.calls == []
    ev = r.tool_events[0]
    assert (ev.status, ev.rule_id, ev.content) == ("denied", "deny.canary", CANARY_DENIED)
    assert audit.recs[0].tool == "scratch_write" and audit.recs[0].reason.startswith("tool_args:")


async def test_clean_tool_args_still_run_under_the_guard() -> None:
    tool = RecordingTool("scratch_write")
    fake = FakeLLM([call("scratch_write", {"text": "buy milk"}), "ok"])
    o = orch(fake, [tool], make_guard())
    await o.run_turn("scratch_write buy milk")
    assert tool.calls == [{"text": "buy milk"}]


async def test_dispatcher_screen_error_denies() -> None:
    class Broken:
        async def screen(self, *a: Any, **k: Any) -> bool:
            raise RuntimeError("bug")

    tool = RecordingTool("t")
    d = Dispatcher({"t": tool}, AllowAll(), canary=Broken())
    res = await d.dispatch("t", {"text": "fine"}, exposed=frozenset({"t"}))
    assert res.rule_id == "deny.canary" and tool.calls == []


def _consumed(tool: str, args: dict[str, Any]) -> ConsumedApproval:
    return seal_consumed(
        action_id=uuid.uuid4(),
        tool=tool,
        action=TOOL_CALL_ACTION,
        payload=args,
        payload_sha256=payload_sha256(tool, TOOL_CALL_ACTION, args),
        step_up_verified=True,
        source_event_id=None,
    )


async def test_approved_call_carrying_a_canary_is_still_denied() -> None:
    tool = RecordingTool("t")
    d = Dispatcher({"t": tool}, AllowAll(), canary=make_guard())
    res = await d.execute_approved(_consumed("t", {"text": CS.value(Site.TOOL_FRAME)}))
    assert res.rule_id == "deny.canary" and tool.calls == [] and res.tainted is True
    ok = await d.execute_approved(_consumed("t", {"text": "fine"}))
    assert ok.status == "ok" and tool.calls == [{"text": "fine"}]


# ------------------------------------------------------------------ egress


async def test_egress_check_blocks_before_any_io_and_passes_clean_requests() -> None:
    transport = RecordingTransport(lambda req: __import__("httpx").Response(200, json={}))
    client = EgressClient(NetSettings(_env_file=None), transport=transport)
    audit = RecordingAudit()
    client.set_outbound_check(egress_check(make_guard(audit=audit)))
    with pytest.raises(RuntimeError):
        client.set_outbound_check(egress_check(make_guard()))  # once only
    drafts = "/gmail/v1/users/me/drafts"
    await client.request(Service.GMAIL, "POST", drafts, json={"message": {"raw": "eA"}})
    bad: list[dict[str, Any]] = [
        {"json": {"t": f"x {CS.value(Site.SYSTEM)}"}},
        {"content": CS.value(Site.MEMORY).encode().hex().encode()},
        {"json": {"raw": base64.b64encode(CS.value(Site.TOOL_FRAME).encode()).decode()}},
    ]
    for kw in bad:
        with pytest.raises(EgressDenied) as ei:
            await client.request(Service.GMAIL, "POST", drafts, **kw)
        assert ei.value.reason == "canary_blocked"
    assert len(transport.calls) == 1  # only the clean request reached the transport
    assert [r.reason for r in audit.recs] == [
        "egress_gmail:system:plain",
        "egress_gmail:memory:hex",
        "egress_gmail:tool_frame:base64",
    ]
    await client.aclose()


async def test_egress_check_errors_and_non_true_fail_closed() -> None:
    transport = RecordingTransport(lambda req: __import__("httpx").Response(200, json={}))
    client = EgressClient(NetSettings(_env_file=None), transport=transport)

    async def weird(service: Service, text: str) -> bool:
        return "yes"  # type: ignore[return-value]

    client.set_outbound_check(weird)
    with pytest.raises(EgressDenied):
        await client.request(
            Service.NOTION, "GET", "/v1/pages/0a1b2c3d-0000-4000-8000-0123456789ab"
        )
    with pytest.raises(TypeError):
        EgressClient(NetSettings(_env_file=None)).set_outbound_check("nope")  # type: ignore[arg-type]
    await client.aclose()


# ------------------------------------------------------------ llm payload


async def test_planted_canaries_reach_the_model_but_not_the_audit_payload() -> None:
    fake = FakeLLM(["ok"])
    env = build_env(provider=fake, store_payload=True)
    from src.llm.guard import GuardedLLM

    llm = GuardedLLM(
        fake,
        flags=env.flags,
        audit=env.audit,
        ledger=env.audit,
        settings=llm_settings(store_payload=True),
        payload_redact=CS.values(),
    )
    line = CS.plant(Site.SYSTEM)
    await llm.chat(
        ChatRequest(
            purpose=Purpose.CHAT,
            lane=Lane.TRUSTED,
            messages=(
                ChatMessage(role="system", content=line),
                ChatMessage(role="user", content="hi"),
            ),
        )
    )
    assert line in (fake.requests[0].messages[0].content or "")
    stored = env.audit.records[-1].payload_redacted or ""
    assert CS.value(Site.SYSTEM) not in stored and "[REDACTED:canary]" in stored


# ----------------------------------------------------------------- runtime


def _cfg(tmp_path: Path, **kw: Any) -> AppConfig:
    return AppConfig.build(
        app=AppSettings(_env_file=None, heartbeat_path=tmp_path / "hb.json"),
        gateway=make_settings(),
        llm=llm_settings(canaries=("CONFIGURED-CANARY-77aa",)),
        net=NetSettings(_env_file=None),
        dsn="postgresql+asyncpg://u:p@127.0.0.1:1/none",
        **kw,
    )


def test_seeded_config_derives_stable_canaries_and_hides_the_seed(tmp_path: Path) -> None:
    cs = CanarySettings(_env_file=None, seed=SecretStr(SEED))  # type: ignore[call-arg]
    cfg = _cfg(tmp_path, canary=cs)
    assert cfg.canaries.values() == derive_canaries(SEED).values()
    assert SEED in cfg.secrets
    assert SEED not in repr(cfg) and not any(v in repr(cfg) for v in cfg.canaries.values())


def test_unseeded_config_gets_random_canaries(tmp_path: Path) -> None:
    a, b = _cfg(tmp_path), _cfg(tmp_path)
    assert a.canaries.values() != b.canaries.values()


async def test_wire_canary_binds_audit_alert_egress_and_wraps_the_gateway(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    guard = build_canary_guard(cfg)
    inner = RecordingGateway()
    egress = EgressClient(NetSettings(_env_file=None), transport=RecordingTransport(lambda r: None))
    audit = RecordingAudit()
    gw = wire_canary(cfg, guard, Backends(gateway=inner, egress=egress), audit)  # type: ignore[arg-type]
    assert isinstance(gw, CanaryGateway)
    await gw.send_text(f"leak {cfg.canaries.value(Site.SYSTEM)}")
    await gw.send_text("configured-canary-77aa")  # operator literal, case folded
    await guard.drain()
    assert inner.texts[0] == BLOCKED_REPLY and inner.texts[1] == BLOCKED_REPLY
    assert ALERT_TEXT in inner.texts  # the alert used the unwrapped gateway
    assert len(audit.recs) == 2
    with pytest.raises(RuntimeError):
        egress.set_outbound_check(egress_check(guard))  # already installed
    await egress.aclose()


async def test_alerts_can_be_switched_off(tmp_path: Path) -> None:
    cs = CanarySettings(_env_file=None, alert_telegram=False)  # type: ignore[call-arg]
    cfg = _cfg(tmp_path, canary=cs)
    guard = build_canary_guard(cfg)
    inner = RecordingGateway()
    gw = wire_canary(cfg, guard, Backends(gateway=inner), RecordingAudit())  # type: ignore[arg-type]
    await gw.send_text(cfg.canaries.value(Site.MEMORY))
    await guard.drain()
    assert inner.texts == [BLOCKED_REPLY]


def test_recording_alert_type_is_callable() -> None:
    assert callable(RecordingAlert())
