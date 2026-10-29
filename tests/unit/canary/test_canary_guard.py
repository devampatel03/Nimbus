# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.4 guard and Telegram decorator: block, audit (fixed codes only), alert
(fixed text, cooldown, background, failure-tolerant), stream split detection,
fail closed on detector errors, log redaction of obfuscated canaries."""

from __future__ import annotations

import asyncio
import base64
import io
import logging

import pytest

from src.canary.gateway import CanaryGateway
from src.canary.guard import ALERT_TEXT, BLOCKED_REPLY, EVENT, RULE_ID, CanaryBlocked
from src.canary.tokens import Site
from src.core.logging import add_log_matchers, install_log_scrubbing
from src.gateway.types import ApprovalCard, MessageRef
from tests._canary_helpers import (
    RecordingAlert,
    RecordingAudit,
    RecordingGateway,
    canaries,
    make_guard,
)

CS = canaries()
V = CS.value(Site.SYSTEM)


def _no_value(text: str) -> None:
    for v in CS.values():
        assert v not in text
        assert v.upper() not in text


async def test_clean_text_passes_without_audit_or_alert() -> None:
    audit, alert = RecordingAudit(), RecordingAlert()
    g = make_guard(audit=audit, alert=alert)
    assert await g.screen("hello there", sink="telegram_send") is True
    await g.drain()
    assert audit.recs == [] and alert.sent == [] and g.hits == 0


async def test_hit_is_audited_with_codes_only_and_alerts_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit, alert = RecordingAudit(), RecordingAlert()
    g = make_guard(audit=audit, alert=alert)
    with caplog.at_level(logging.INFO):
        assert await g.screen(f"pwned {V}", sink="telegram_send", tool="x") is False
        assert await g.screen(base64.b64encode(V.encode()).decode(), sink="tool_args") is False
    await g.drain()
    assert [r.event for r in audit.recs] == [EVENT, EVENT]
    rec = audit.recs[0]
    assert rec.rule_id == RULE_ID and rec.outcome == "deny" and rec.executed is False
    assert rec.reason == "telegram_send:system:plain" and rec.tool == "x"
    assert audit.recs[1].reason == "tool_args:system:base64"
    assert alert.sent == [ALERT_TEXT]  # second hit inside the cooldown: no second alert
    _no_value(repr(audit.recs) + caplog.text + ALERT_TEXT)
    assert g.hits == 2


async def test_alert_cooldown_expires() -> None:
    now = [0.0]
    alert = RecordingAlert()
    g = make_guard(alert=alert, clock=lambda: now[0], cooldown=60)
    await g.screen(V, sink="s")
    now[0] = 30
    await g.screen(V, sink="s")
    now[0] = 61
    await g.screen(V, sink="s")
    await g.drain()
    assert alert.sent == [ALERT_TEXT, ALERT_TEXT]


async def test_audit_and_alert_failures_still_block(caplog: pytest.LogCaptureFixture) -> None:
    g = make_guard(audit=RecordingAudit(fail=True), alert=RecordingAlert(fail=True))
    with caplog.at_level(logging.WARNING):
        assert await g.screen(V, sink="s") is False
        await g.drain()
    assert "canary audit write failed" in caplog.text and "canary alert failed" in caplog.text


async def test_detector_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    g = make_guard()

    def boom(_: str) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(g.detector, "detect", boom)
    assert await g.screen("totally benign", sink="s") is False
    assert g.log_matcher("benign") is True


async def test_canary_split_across_consecutive_sends_is_caught() -> None:
    audit = RecordingAudit()
    g = make_guard(audit=audit)
    pieces = [V[i : i + 6] for i in range(0, 18, 6)]  # 6-char pieces: each alone is clean
    results = [await g.screen(p, sink="telegram_send", stream=True) for p in pieces]
    assert results[0] is True and False in results
    assert audit.recs[-1].reason.endswith(":split")


async def test_stateless_calls_do_not_join_pieces() -> None:
    g = make_guard()
    assert await g.screen(V[:6], sink="s", stream=False) is True
    assert await g.screen(V[6:12], sink="s", stream=False) is True  # no tail without stream


async def test_require_clean_raises_fixed_error() -> None:
    g = make_guard()
    with pytest.raises(CanaryBlocked) as ei:
        await g.require_clean(f"x {V}", sink="telegram_card")
    _no_value(str(ei.value))


async def test_alert_without_running_loop_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    g = make_guard(alert=RecordingAlert())
    with caplog.at_level(logging.WARNING):
        await asyncio.to_thread(g._maybe_alert)  # a worker thread has no running loop
    assert "no running loop" in caplog.text


# ------------------------------------------------------------- the gateway


async def test_gateway_replaces_a_leaking_reply_and_passes_clean_ones() -> None:
    inner, audit = RecordingGateway(), RecordingAudit()
    gw = CanaryGateway(inner, make_guard(audit=audit))
    await gw.send_text("all good")
    await gw.send_text(f"the secret is {' '.join(V)}", reply_to=MessageRef(1))
    assert inner.texts == ["all good", BLOCKED_REPLY]
    assert audit.recs[0].reason.startswith("telegram_send:")
    assert gw.inner is inner


async def test_gateway_refuses_cards_and_keypads_and_drops_toasts() -> None:
    inner = RecordingGateway()
    gw = CanaryGateway(inner, make_guard())
    with pytest.raises(CanaryBlocked):
        await gw.send_approval(ApprovalCard(f"send {V}", "a", "r"))
    with pytest.raises(CanaryBlocked):
        await gw.send_keypad("enter code", [[(V, "cb")]])
    with pytest.raises(CanaryBlocked):
        await gw.close_approval(MessageRef(1), outcome=V)
    await gw.answer_callback("cb1", f"done {V}")
    await gw.answer_callback("cb2", "ok")
    assert inner.cards == [] and inner.keypads == [] and inner.closed == []
    assert inner.toasts == [None, "ok"]
    await gw.send_approval(ApprovalCard("pay rent", "a", "r"))
    await gw.send_keypad("enter code", [[("1", "cb")]])
    await gw.close_approval(MessageRef(1), outcome="approved")
    assert (
        len(inner.cards) == 1 and inner.keypads == ["enter code"] and inner.closed == ["approved"]
    )


async def test_gateway_passthrough_inbound_ack_fetch() -> None:
    inner = RecordingGateway()
    gw = CanaryGateway(inner, make_guard())
    assert [u async for u in gw.inbound()] == []
    await gw.ack(None)  # type: ignore[arg-type]
    assert await gw.fetch_file(None, max_bytes=1) == b""  # type: ignore[arg-type]


# ----------------------------------------------------------------- logging


def test_log_scrubber_redacts_obfuscated_canaries() -> None:
    g = make_guard()
    buf = io.StringIO()
    lg = logging.getLogger("p44.canary.logtest")
    lg.handlers[:] = [logging.StreamHandler(buf)]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    restore = install_log_scrubbing(CS.values())
    try:
        add_log_matchers((g.log_matcher,))
        lg.info("plain %s", V)
        lg.info("spaced %s", " ".join(V))
        lg.info("b64 %s", base64.b64encode(V.encode()).decode())
        lg.info("zw %s", "\u200b".join(V))
        lg.info("benign line")
    finally:
        restore()
    out = buf.getvalue()
    _no_value(out)
    assert " ".join(V) not in out and base64.b64encode(V.encode()).decode() not in out
    assert out.count("[REDACTED:canary]") >= 3 and "benign line" in out


async def test_pieces_sent_out_of_order_are_caught_as_scattered() -> None:
    audit = RecordingAudit()
    g = make_guard(audit=audit)
    c1, c3 = V[0:8], V[16:24]
    assert await g.screen(c1, sink="telegram_send", stream=True) is True
    assert await g.screen("unrelated chatter in between", sink="telegram_send", stream=True)
    assert await g.screen(c3, sink="telegram_send", stream=True) is False
    assert audit.recs[-1].reason.endswith(":scattered")
    # The window is kept after a hit: the next piece is blocked as well.
    assert await g.screen(V[8:16], sink="telegram_send", stream=True) is False


async def test_a_long_benign_stream_never_trips() -> None:
    g = make_guard()
    lines = [
        f"Reminder {i}: call the bank about account ending {1000 + i} at {i % 12}pm."
        for i in range(300)
    ]
    for line in lines:
        assert await g.screen(line, sink="telegram_send", stream=True) is True
