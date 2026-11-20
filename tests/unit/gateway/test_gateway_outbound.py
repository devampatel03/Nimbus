# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Outbound path: previews off, escaping, chunking, 429, approvals, files,
flood pacing, token hygiene."""

from __future__ import annotations

import ast
import logging
import pathlib

import httpx
import pytest

from src.gateway import ApprovalCard, FileRef, MessageRef, RateLimiter, TelegramGateway, UpdateKind
from src.gateway.settings import GatewaySettings
from src.gateway.types import FileTooLarge, MessageTooLong, TelegramError, TelegramRateLimited
from tests.unit.gateway.fake_store import MemStore
from tests.unit.gateway.fake_telegram import (
    TOKEN,
    USER,
    FakeTelegram,
    make_egress,
    make_settings,
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def build(fake: FakeTelegram, clock: Clock | None = None, **kw: object) -> TelegramGateway:
    clock = clock or Clock()
    s = make_settings(**kw)
    limiter = RateLimiter(
        per_chat_interval_s=s.per_chat_interval_s,
        global_interval_s=s.global_interval_s,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return TelegramGateway(s, make_egress(fake), MemStore(), limiter=limiter, sleep=clock.sleep)


async def test_every_send_disables_link_previews_and_uses_html() -> None:
    fake = FakeTelegram()
    gw = build(fake)
    await gw.send_text("see https://example.com and http://x.test")
    await gw.send_text("p1\n\n" + "x" * 5000)
    await gw.send_approval(ApprovalCard("do thing", "ap:aaa", "rj:aaa"))
    assert len(fake.sent) >= 4
    for body in fake.sent:
        assert body["link_preview_options"] == {"is_disabled": True}
        assert body["parse_mode"] == "HTML"
        assert body["chat_id"] == USER


async def test_hostile_text_is_escaped_on_the_wire() -> None:
    fake = FakeTelegram()
    gw = build(fake)
    await gw.send_text('<a href="https://evil.example">x</a> & <b>bold</b>')
    (body,) = fake.sent
    assert "<a" not in body["text"] and "<b>" not in body["text"]
    assert (
        body["text"] == '&lt;a href="[link removed]"&gt;x&lt;/a&gt; &amp; &lt;b&gt;bold&lt;/b&gt;'
    )  # P4.3: the URL itself is also neutralised before escaping


async def test_long_message_is_chunked_under_4096_and_in_order() -> None:
    fake = FakeTelegram()
    gw = build(fake)
    text = "\n\n".join(f"para{i} " + "z" * 1000 for i in range(20))
    refs = await gw.send_text(text)
    assert len(refs) == len(fake.sent) > 4
    assert all(len(b["text"]) <= 4096 for b in fake.sent)
    assert [r.message_id for r in refs] == sorted(r.message_id for r in refs)


async def test_reply_to_only_on_first_chunk() -> None:
    fake = FakeTelegram()
    gw = build(fake)
    await gw.send_text("a" * 5000, reply_to=MessageRef(9))
    assert fake.sent[0]["reply_parameters"]["message_id"] == 9
    assert all("reply_parameters" not in b for b in fake.sent[1:])


async def test_empty_text_sends_nothing() -> None:
    fake = FakeTelegram()
    assert await build(fake).send_text("  \n ") == []
    assert fake.sent == []


async def test_flood_is_paced_to_one_per_second_per_chat() -> None:
    fake = FakeTelegram()
    clock = Clock()
    gw = build(fake, clock)
    for i in range(60):
        await gw.send_text(f"m{i}")
    assert len(fake.sent) == 60
    assert clock.t >= 58.9


async def test_429_retry_after_is_honoured_then_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeTelegram()
    fake.scripted = [
        httpx.Response(
            429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}
        ),
    ]
    clock = Clock()
    gw = build(fake, clock)
    with caplog.at_level(logging.WARNING):
        refs = await gw.send_text("hello")
    assert len(refs) == 1
    assert 7.0 in clock.sleeps
    assert TOKEN not in "\n".join(r.getMessage() for r in caplog.records)


async def test_429_beyond_budget_raises_sanitised() -> None:
    fake = FakeTelegram()
    r = httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
    fake.scripted = [r, r, r, r, r]
    gw = build(fake, max_429_retries=2)
    with pytest.raises(TelegramRateLimited) as ei:
        await gw.send_text("hello")
    assert TOKEN not in str(ei.value) and TOKEN not in repr(ei.value)


async def test_429_with_absurd_retry_after_is_not_slept_on() -> None:
    fake = FakeTelegram()
    fake.scripted = [httpx.Response(429, json={"parameters": {"retry_after": 10**9}})]
    clock = Clock()
    gw = build(fake, clock)
    with pytest.raises(TelegramRateLimited):
        await gw.send_text("hello")
    assert max(clock.sleeps, default=0) < 1000


async def test_api_error_is_sanitised() -> None:
    fake = FakeTelegram()
    fake.scripted = [httpx.Response(400, json={"ok": False, "description": "bad"})]
    with pytest.raises(TelegramError) as ei:
        await build(fake).send_text("x")
    assert TOKEN not in str(ei.value)


async def test_approval_card_shows_exact_payload_and_signed_buttons() -> None:
    fake = FakeTelegram()
    payload = 'create_event(title="Dinner <b>", start=2026-10-26T19:00+05:30)'
    token = "ap:" + "A" * 38
    await build(fake).send_approval(ApprovalCard(payload, token, "rj:" + "A" * 38))
    (body,) = fake.sent
    assert "<pre>" in body["text"]
    assert 'title="Dinner &lt;b&gt;"' in body["text"] and "2026-10-26T19:00+05:30" in body["text"]
    kb = body["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb] == [token, "rj:" + "A" * 38]
    assert all(len(b["callback_data"].encode()) <= 64 for b in kb)


@pytest.mark.parametrize("bad", ["", "x" * 65, "é" * 33])
async def test_callback_data_over_64_bytes_or_empty_is_refused(bad: str) -> None:
    fake = FakeTelegram()
    with pytest.raises(ValueError, match="64"):
        await build(fake).send_approval(ApprovalCard("p", bad, "rj:x"))
    assert fake.sent == []


async def test_oversize_approval_payload_is_refused_not_truncated() -> None:
    fake = FakeTelegram()
    with pytest.raises(MessageTooLong):
        await build(fake).send_approval(ApprovalCard("x" * 5000, "ap:a", "rj:a"))
    assert fake.sent == []


async def test_close_approval_removes_buttons_and_reports() -> None:
    fake = FakeTelegram()
    await build(fake).close_approval(MessageRef(55), outcome="approved")
    assert fake.methods()[0] == "editMessageReplyMarkup"
    assert fake.requests[0][1]["reply_markup"] == {"inline_keyboard": []}
    assert fake.sent[0]["link_preview_options"] == {"is_disabled": True}


async def test_answer_callback() -> None:
    fake = FakeTelegram()
    await build(fake).answer_callback("cb1", "ok<b>")
    method, body = fake.requests[-1]
    assert method == "answerCallbackQuery" and body["callback_query_id"] == "cb1"


def ref(kind: UpdateKind = UpdateKind.DOCUMENT, size: int | None = 10, fid: str = "f1") -> FileRef:
    return FileRef(fid, "u", size, None, "a.txt", kind)


async def test_fetch_file_ok() -> None:
    fake = FakeTelegram()
    fake.files["docs/f1"] = b"hello transcript"
    got = await build(fake).fetch_file(ref(), max_bytes=1000)
    assert got == b"hello transcript"


async def test_fetch_refuses_before_any_request_when_declared_size_too_big() -> None:
    fake = FakeTelegram()
    with pytest.raises(FileTooLarge):
        await build(fake).fetch_file(ref(size=2_000_000), max_bytes=20 * 1024 * 1024)
    assert fake.requests == []  # own document cap (1 MB) enforced pre-download


async def test_fetch_enforces_own_voice_cap_below_telegram_cap() -> None:
    fake = FakeTelegram()
    with pytest.raises(FileTooLarge):
        await build(fake).fetch_file(
            ref(UpdateKind.VOICE, size=15 * 1024 * 1024), max_bytes=20 * 1024 * 1024
        )
    assert fake.requests == []


async def test_fetch_refuses_when_getfile_reports_bigger_than_declared() -> None:
    fake = FakeTelegram()
    fake.file_sizes["f1"] = 5_000_000
    with pytest.raises(FileTooLarge):
        await build(fake).fetch_file(ref(size=None), max_bytes=1000)
    assert "getFile" in fake.methods()
    assert not any(u.startswith("https://api.telegram.org/file/") for u in fake.raw_urls)


async def test_fetch_aborts_oversized_body_even_if_size_lied() -> None:
    fake = FakeTelegram()
    fake.files["docs/f1"] = b"x" * 5000
    with pytest.raises(FileTooLarge):
        await build(fake).fetch_file(ref(size=10), max_bytes=1000)


@pytest.mark.parametrize("path", ["../../etc/passwd", "a/../b", "/abs", "a b", "a?x=1", "a#b"])
async def test_fetch_rejects_hostile_file_path_from_api(path: str) -> None:
    fake = FakeTelegram()
    fake.scripted = [
        httpx.Response(200, json={"ok": True, "result": {"file_id": "f1", "file_path": path}})
    ]
    with pytest.raises(TelegramError):
        await build(fake).fetch_file(ref(), max_bytes=1000)
    assert not any("/file/" in u for u in fake.raw_urls)


def test_settings_reject_bad_tokens_without_echoing_them() -> None:
    for bad in ["", "abc", "123:short", "123456789:" + "a/b" * 10, "123456789:" + "a" * 30 + "\n"]:
        with pytest.raises(ValueError) as ei:
            GatewaySettings(_env_file=None, bot_token=bad, allowed_user_id=1)  # type: ignore[arg-type]
        assert bad not in str(ei.value) or bad == ""


def test_settings_repr_hides_token() -> None:
    assert TOKEN not in repr(make_settings())
    assert TOKEN not in str(make_settings())


def test_gateway_package_imports_no_http_library() -> None:
    """INV-1 / ADR-0004: only src.net may import an HTTP transport."""
    banned = {
        "httpx",
        "requests",
        "aiohttp",
        "urllib3",
        "http",
        "urllib",
        "socket",
        "telegram",
        "aiogram",
        "openai",
    }
    for p in pathlib.Path("src/gateway").glob("*.py"):
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            mods: list[str] = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods = [n.module]
            for m in mods:
                assert m.split(".")[0] not in banned, (p.name, m)


def test_gateway_never_calls_install_log_scrubbing() -> None:
    for p in pathlib.Path("src/gateway").glob("*.py"):
        assert "install_log_scrubbing" not in p.read_text(encoding="utf-8")


# ---- round 1 review: M4, L1, L3, L4 ----


async def test_429_penalizes_limiter_so_next_send_is_pushed_back() -> None:
    fake = FakeTelegram()
    fake.scripted = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}}),
    ]

    class Frozen(Clock):
        async def sleep(self, s: float) -> None:  # time does not pass: isolates the limiter
            self.sleeps.append(s)

    clock = Frozen()
    gw = build(fake, clock)
    await gw.send_text("a")  # 429 -> api sleeps 7 -> retry ok
    clock.sleeps.clear()
    await gw.send_text("b")
    assert clock.sleeps and clock.sleeps[0] >= 7.0 - 1e-9


@pytest.mark.parametrize("bad", [float("nan"), -5, float("-inf"), True, "7", None])
async def test_retry_after_nonsense_falls_back_to_one_second(bad: object) -> None:
    fake = FakeTelegram()
    fake.scripted = [
        httpx.Response(429, content=_json({"ok": False, "parameters": {"retry_after": bad}}))
    ]
    clock = Clock()
    gw = build(fake, clock)
    await gw.send_text("x")
    assert 1.0 in clock.sleeps
    assert all(s >= 0 for s in clock.sleeps)


def _json(obj: object) -> bytes:
    import json

    return json.dumps(obj).encode()  # allow_nan default True: emits NaN/-Infinity


async def test_edit_and_answer_callback_go_through_the_limiter() -> None:
    fake = FakeTelegram()
    clock = Clock()
    gw = build(fake, clock)
    await gw.answer_callback("cb1")
    await gw.answer_callback("cb2")
    await gw.close_approval(MessageRef(5), outcome="approved")
    assert clock.t == pytest.approx(3.0)  # 4 limiter slots at 1 per second


async def test_chunks_respect_utf16_code_units() -> None:
    fake = FakeTelegram()
    gw = build(fake)
    await gw.send_text("😀" * 4096)  # 4096 chars, 8192 UTF-16 units
    assert len(fake.sent) >= 2
    for body in fake.sent:
        assert len(body["text"].encode("utf-16-le")) // 2 <= 4096
