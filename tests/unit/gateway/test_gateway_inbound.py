# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Inbound path: R2 allowlist, redelivery, hostile updates, backoff."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
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


# ---- round 1 review: M1/M2 (R2 chat checks), L2 (untrusted origins) ----


@pytest.mark.parametrize(
    "chat",
    [
        {"type": "private", "id": 777},  # allowed sender, someone else's private chat
        {"type": "group", "id": USER},  # right id, wrong chat type
        {"type": "private"},
        {"type": "private", "id": True},
        None,
        "private",
    ],
)
def test_message_from_allowed_user_in_wrong_chat_is_dropped(chat: Any) -> None:
    raw = message(1, "x")
    raw["message"]["chat"] = chat
    p = parse_update(raw, USER)
    assert p is not None and p.kind is UpdateKind.DROPPED and p.update is None


@pytest.mark.parametrize(
    "msg",
    [
        None,
        {"message_id": 7},  # no chat
        {"message_id": 7, "chat": {"type": "group", "id": USER}},
        {"message_id": 7, "chat": {"type": "private", "id": 777}},
        "str",
    ],
)
def test_callback_requires_message_in_users_private_chat(msg: Any) -> None:
    raw = callback(1)
    if msg is None:
        del raw["callback_query"]["message"]
    else:
        raw["callback_query"]["message"] = msg
    p = parse_update(raw, USER)
    assert p is not None and p.kind is UpdateKind.DROPPED and p.update is None


def test_inline_message_callback_is_dropped() -> None:
    raw = callback(1)
    del raw["callback_query"]["message"]
    raw["callback_query"]["inline_message_id"] = "abc"
    p = parse_update(raw, USER)
    assert p is not None and p.update is None


@pytest.mark.parametrize(
    "key",
    [
        "forward_origin",
        "forward_date",
        "forward_from",
        "forward_from_chat",
        "via_bot",
        "is_automatic_forward",
    ],
)
def test_third_party_origin_markers_make_text_untrusted(key: str) -> None:
    raw = message(1, "hi")
    raw["message"][key] = {"x": 1} if key != "forward_date" else 1700000000
    p = parse_update(raw, USER)
    assert p and p.update and p.update.untrusted


def test_forwarded_voice_untrusted() -> None:
    raw = message(1, "")
    del raw["message"]["text"]
    raw["message"]["voice"] = {"file_id": "f", "file_unique_id": "u"}
    raw["message"]["via_bot"] = {"id": 1}
    p = parse_update(raw, USER)
    assert p and p.update and p.update.untrusted


# ---- H1: poison update / redelivery budget / stalled-poll backoff ----


async def test_poison_update_is_dropped_after_budget_and_queue_drains(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeTelegram()
    fake.push(*(message(i, f"SECRETTEXT{i}") for i in range(1, 151)))
    store = MemStore(max_deliveries=3)
    gw, _ = build(fake, store)
    delivered: list[int] = []
    with caplog.at_level(logging.INFO, logger="nimbus.gateway"):
        async for u in gw.inbound():
            delivered.append(u.update_id)
            if u.update_id != 1:  # update 1 is poison: never acked
                await gw.ack(u)
            if {*range(2, 151)} <= set(delivered):
                await gw.aclose()
            if len(delivered) > 2000:
                pytest.fail("hot loop")
    assert delivered.count(1) == 3  # exactly the budget
    assert store.rows[1] is True and store.offset == 151
    assert {*range(2, 151)} <= set(delivered)  # >100 queued behind it all got through
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "poison" in text and "update_id=1" in text and "SECRETTEXT" not in text
    assert fake.methods().count("getUpdates") < 20


async def test_stalled_polls_back_off_instead_of_hot_looping() -> None:
    fake = FakeTelegram()
    fake.push(message(1, "a"))
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    gw = TelegramGateway(
        make_settings(), make_egress(fake), MemStore(max_deliveries=8), sleep=sleep
    )
    n = 0
    async for _ in gw.inbound():  # never ack
        n += 1
        if n == 5:
            await gw.aclose()
    assert sleeps[:3] == [1.0, 2.0, 4.0]  # exponential, no zero-delay re-poll
    assert max(sleeps) <= 30.0


async def test_backoff_resets_after_progress() -> None:
    fake = FakeTelegram()
    fake.push(message(1, "a"))
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    gw = TelegramGateway(make_settings(), make_egress(fake), MemStore(), sleep=sleep)
    seen: list[int] = []
    async for u in gw.inbound():
        seen.append(u.update_id)
        if len(seen) == 2:
            await gw.ack(u)
            fake.push(message(2, "b"))
        elif len(seen) == 3:
            await gw.ack(u)
            await gw.aclose()
    assert sleeps == [1.0]  # one stall, then progress, no further waits


def test_poll_timeout_must_be_at_least_one_second() -> None:
    with pytest.raises(ValueError):
        make_settings(poll_timeout_s=0)
    assert make_settings(poll_timeout_s=1).poll_timeout_s == 1


async def test_conflict_409_logged_as_distinct_reason(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeTelegram()
    fake.scripted = [httpx.Response(409, json={"ok": False, "error_code": 409})]
    fake.push(message(1))
    gw, _ = build(fake)
    with caplog.at_level(logging.WARNING, logger="nimbus.gateway"):
        await take(gw, 1)
    assert any("reason=conflict" in r.getMessage() for r in caplog.records)


# ---- M1 (P2.1b): minimum age before poison + fixed user notice ----

NOTICE = "I couldn't process a message you sent at 10:00, please resend"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 4, 30, tzinfo=UTC)  # 10:00 IST

    def __call__(self) -> datetime:
        return self.now


def _close_once_sent(fake: FakeTelegram, gw: TelegramGateway) -> None:
    def hook() -> None:
        if fake.sent:
            asyncio.get_running_loop().create_task(gw.aclose())

    fake.poll_hook = hook


async def test_poison_waits_for_minimum_age_then_sends_fixed_notice() -> None:
    clock = _Clock()
    fake = FakeTelegram()
    fake.push(message(1, "TOPSECRET body"))
    store = MemStore(max_deliveries=3, poison_min_age_s=600, clock=clock)
    gw, _ = build(fake, store)
    _close_once_sent(fake, gw)
    n = 0
    async for u in gw.inbound():  # never ack
        n += 1
        if n == 10:  # far past the delivery budget, but the update is 0 min old
            assert store.rows[1] is False and fake.sent == []
            clock.now += timedelta(minutes=11)
        assert n < 100, "hot loop"
    assert store.rows[1] is True
    assert [m["text"] for m in fake.sent] == [NOTICE]
    assert "TOPSECRET" not in str(fake.sent)
    assert store.notices == {}  # cleared after a successful send


async def test_young_update_is_never_poisoned_however_many_redeliveries() -> None:
    fake = FakeTelegram()
    fake.push(message(1, "legit"))
    store = MemStore(max_deliveries=2, poison_min_age_s=600, clock=_Clock())
    gw, _ = build(fake, store)
    n = 0
    async for _ in gw.inbound():
        n += 1
        if n == 30:
            await gw.aclose()
    assert store.rows[1] is False and fake.sent == []


async def test_failed_notice_is_retried_not_lost() -> None:
    fake = FakeTelegram()
    fake.push(message(1, "x"))
    store = MemStore(max_deliveries=1, poison_min_age_s=0, clock=_Clock())
    gw, _ = build(fake, store)
    polls = 0

    def hook() -> None:
        nonlocal polls
        polls += 1
        if polls == 2:  # the next request is the notice send: make it fail once
            fake.scripted.append(httpx.Response(500, json={"ok": False}))
        if fake.sent:
            asyncio.get_running_loop().create_task(gw.aclose())

    fake.poll_hook = hook
    n = 0
    async for _ in gw.inbound():
        n += 1
        assert n < 100, "hot loop"
    assert [m["text"] for m in fake.sent] == [NOTICE]
    assert store.notices == {}


# ---- P2.1b MED-4: liveness hook fires on completed poll cycles only ----


async def test_on_poll_fires_for_each_completed_poll_including_empty_ones() -> None:
    fake = FakeTelegram()
    polls = 0
    beats: list[int] = []

    def hook() -> None:
        nonlocal polls
        polls += 1
        if polls == 3:
            fake.push(message(1, "x"))

    fake.poll_hook = hook
    gw = TelegramGateway(
        make_settings(),
        make_egress(fake),
        MemStore(),
        sleep=no_sleep,
        on_poll=lambda: beats.append(1),
    )
    async for u in gw.inbound():
        await gw.ack(u)
        await gw.aclose()
    assert len(beats) >= 3  # two empty long polls and the one that returned the update


async def test_on_poll_does_not_fire_when_the_poll_fails() -> None:
    fake = FakeTelegram()
    fake.scripted = [httpx.Response(500, json={"ok": False}) for _ in range(3)]
    beats: list[int] = []
    gw = TelegramGateway(
        make_settings(),
        make_egress(fake),
        MemStore(),
        sleep=no_sleep,
        on_poll=lambda: beats.append(1),
    )
    n = 0

    def stop_after_failures() -> None:
        nonlocal n
        n += 1
        if n == 1:
            asyncio.get_running_loop().create_task(gw.aclose())

    fake.poll_hook = stop_after_failures
    async for _ in gw.inbound():
        pass
    assert len(beats) == 1  # only the first successful poll; the 3 failures beat nothing
