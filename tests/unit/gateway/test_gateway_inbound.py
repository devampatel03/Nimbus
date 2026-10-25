# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Inbound path: R2 allowlist, redelivery, hostile updates, backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from src.gateway import InboundUpdate, TelegramGateway, UpdateKind
from src.gateway.parse import parse_update
from src.gateway.types import TelegramAuthError
from tests.unit.gateway.fake_store import MemStore
from tests.unit.gateway.fake_telegram import (
    TOKEN,
    USER,
    FakeTelegram,
    callback,
    make_egress,
    make_settings,
    message,
)

STRANGER = 666


async def no_sleep(_: float) -> None:
    return None


def build(fake: FakeTelegram, store: MemStore | None = None) -> tuple[TelegramGateway, MemStore]:
    store = store or MemStore()
    return TelegramGateway(make_settings(), make_egress(fake), store, sleep=no_sleep), store


async def take(gw: TelegramGateway, n: int, *, ack: bool = True) -> list[InboundUpdate]:
    out: list[InboundUpdate] = []
    async for u in gw.inbound():
        out.append(u)
        if ack:
            await gw.ack(u)
        if len(out) >= n:
            await gw.aclose()
    return out


async def test_allowed_user_is_served() -> None:
    fake = FakeTelegram()
    fake.push(message(1, "hello"))
    gw, store = build(fake)
    got = await take(gw, 1)
    assert [(u.update_id, u.kind, u.text) for u in got] == [(1, UpdateKind.TEXT, "hello")]
    assert store.offset == 2


async def test_stranger_message_and_callback_get_silence_and_scrubbed_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeTelegram()
    fake.push(
        message(1, "SECRET-BODY-FROM-STRANGER", user=STRANGER),
        callback(2, "ap:forged", user=STRANGER),
        message(3, "mine"),
    )
    gw, store = build(fake)
    with caplog.at_level(logging.INFO, logger="nimbus.gateway"):
        got = await take(gw, 1)
    assert [u.update_id for u in got] == [3]
    # silence: nothing but getUpdates was ever sent
    assert set(fake.methods()) == {"getUpdates"}
    assert fake.sent == []
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET-BODY" not in text and "ap:forged" not in text and TOKEN not in text
    assert f"from_id={STRANGER}" in text and "update_id=1" in text and "update_id=2" in text
    # strangers' updates still advance the offset
    assert store.offset == 4


async def test_allowlist_on_message_in_other_chat_type() -> None:
    fake = FakeTelegram()
    u = message(1, "x")
    u["message"]["chat"] = {"id": -100123, "type": "supergroup"}
    fake.push(u, message(2, "ok"))
    gw, _ = build(fake)
    got = await take(gw, 1)
    assert [x.update_id for x in got] == [2]


async def test_callback_from_allowed_user_is_delivered() -> None:
    fake = FakeTelegram()
    fake.push(callback(1, "ap:tok"))
    gw, _ = build(fake)
    (u,) = await take(gw, 1)
    assert u.kind is UpdateKind.CALLBACK and u.callback_data == "ap:tok" and u.callback_id == "cb1"


_PRIV = {"id": USER, "type": "private"}
HOSTILE_UPDATES: list[Any] = [
    None,
    42,
    "x",
    [],
    {},
    {"update_id": True},
    {"update_id": "7"},
    {"update_id": -1},
    {"update_id": 5},
    {"update_id": 5, "message": "str"},
    {"update_id": 5, "message": {"from": {"id": True}}},
    {"update_id": 5, "message": {"from": {"id": "4242"}}},
    {"update_id": 5, "message": {"from": {"id": USER}, "chat": None}},
    {"update_id": 5, "message": {"from": {"id": USER}, "chat": _PRIV}},
    {"update_id": 5, "callback_query": {"from": {"id": USER}}},
    {"update_id": 5, "callback_query": {"from": {"id": USER}, "id": 1, "data": 2}},
    {"update_id": 5, "edited_message": {"from": {"id": STRANGER}}},
    {"update_id": 5, "message": {"from": {"id": USER}, "chat": _PRIV, "voice": {"file_id": 1}}},
]


@pytest.mark.parametrize("raw", HOSTILE_UPDATES)
def test_parse_never_raises_and_never_delivers_garbage(raw: Any) -> None:
    p = parse_update(raw, USER)
    assert p is None or p.update is None


def test_parse_edited_message_from_stranger_is_dropped_not_unsupported() -> None:
    p = parse_update({"update_id": 5, "edited_message": {"from": {"id": STRANGER}}}, USER)
    assert p is not None and p.kind is UpdateKind.DROPPED


def test_forward_is_untrusted_and_recorder_txt_is_untrusted() -> None:
    fwd = message(1, "forwarded text")
    fwd["message"]["forward_origin"] = {"type": "user"}
    p = parse_update(fwd, USER)
    assert p and p.update and p.update.untrusted
    doc = message(2, "")
    del doc["message"]["text"]
    doc["message"]["document"] = {
        "file_id": "f",
        "file_unique_id": "u",
        "file_name": "rec.txt",
        "mime_type": "text/plain",
        "file_size": 10,
    }
    p = parse_update(doc, USER)
    assert p and p.update and p.update.untrusted and p.update.kind is UpdateKind.DOCUMENT
    typed = parse_update(message(3), USER)
    assert typed and typed.update and not typed.update.untrusted


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("a.exe", "application/octet-stream"),
        ("a.html", "text/html"),
        ("a.txt", "text/html"),
        ("a.txt.exe", "text/plain"),
    ],
)
def test_non_txt_documents_unsupported(name: str, mime: str) -> None:
    doc = message(1, "")
    del doc["message"]["text"]
    doc["message"]["document"] = {
        "file_id": "f",
        "file_unique_id": "u",
        "file_name": name,
        "mime_type": mime,
    }
    p = parse_update(doc, USER)
    assert p and p.update is None and p.kind is UpdateKind.UNSUPPORTED


def test_voice_parsed_and_file_id_hidden_from_repr() -> None:
    v = message(1, "")
    del v["message"]["text"]
    v["message"]["voice"] = {
        "file_id": "SECRETFILEID",
        "file_unique_id": "u",
        "file_size": 99,
        "mime_type": "audio/ogg",
    }
    p = parse_update(v, USER)
    assert p and p.update and p.update.file and p.update.file.kind is UpdateKind.VOICE
    assert "SECRETFILEID" not in repr(p.update)


async def test_hostile_updates_in_stream_do_not_stop_polling() -> None:
    fake = FakeTelegram()
    fake.push(
        {"update_id": 1, "message": "garbage"},
        {"update_id": 2, "message": {"from": {"id": USER}, "chat": None}},
        message(3, "fine"),
    )
    gw, _ = build(fake)
    got = await take(gw, 1)
    assert [u.update_id for u in got] == [3]


async def test_unacked_update_is_redelivered_not_skipped() -> None:
    fake = FakeTelegram()
    fake.push(message(1, "a"))
    gw, store = build(fake)
    seen: list[int] = []
    async for u in gw.inbound():
        seen.append(u.update_id)
        if len(seen) == 2:
            await gw.ack(u)
            await gw.aclose()
    assert seen == [1, 1]  # not acked the first time -> resent by Telegram, redelivered
    assert store.offset == 2


async def test_already_processed_update_resent_is_not_redelivered() -> None:
    fake = FakeTelegram()
    store = MemStore()
    store.rows[1] = True
    store.offset = 0  # lagging offset: Telegram resends 1
    fake.push(message(1, "a"), message(2, "b"))
    gw, _ = build(fake, store)
    got = await take(gw, 1)
    assert [u.update_id for u in got] == [2]


async def test_poll_error_backs_off_and_recovers(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeTelegram()
    fake.scripted = [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(409, json={"ok": False}),
    ]
    fake.push(message(1))
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    gw = TelegramGateway(make_settings(), make_egress(fake), MemStore(), sleep=sleep)
    with caplog.at_level(logging.WARNING):
        got = await take(gw, 1)
    assert [u.update_id for u in got] == [1]
    assert sleeps == [1.0, 2.0]
    assert TOKEN not in "\n".join(r.getMessage() for r in caplog.records)


async def test_bad_token_stops_loop() -> None:
    fake = FakeTelegram()
    fake.scripted = [httpx.Response(401, json={"ok": False, "error_code": 401})]
    gw, _ = build(fake)
    with pytest.raises(TelegramAuthError) as ei:
        await take(gw, 1)
    assert TOKEN not in str(ei.value)


async def test_close_stops_idle_poll_loop() -> None:
    fake = FakeTelegram()
    gw, _ = build(fake)

    async def run() -> None:
        async for _ in gw.inbound():
            pass

    t = asyncio.create_task(run())
    await asyncio.sleep(0)
    await gw.aclose()
    await asyncio.wait_for(t, 5)


def test_no_inbound_ports() -> None:
    """R3: the gateway package never binds or listens."""
    import ast
    import pathlib

    banned = {"listen", "bind", "serve_forever", "start_server", "create_server", "uvicorn"}
    for p in pathlib.Path("src/gateway").glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute | ast.Name):
                name = n.attr if isinstance(n, ast.Attribute) else n.id
                assert name not in banned, (p, name)
