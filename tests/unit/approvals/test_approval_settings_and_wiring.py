# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P3.2 settings (hidden secrets, INV-7), gateway keypad / callback sender,
turn-worker routing of callbacks, and runtime wiring of approvals."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from src.app.fakes import InMemoryGateway
from src.app.pipeline import STALE_CALLBACK_MESSAGE, PendingTurn, TurnWorker
from src.app.runtime import AppConfig, build_approvals
from src.app.settings import AppSettings
from src.approvals.settings import ApprovalSettings
from src.gateway.parse import parse_update
from src.gateway.types import InboundUpdate, MessageTooLong, UpdateKind
from src.llm.settings import LLMSettings
from src.net import NetSettings
from tests.unit.gateway.fake_telegram import USER, FakeTelegram, make_egress, make_settings

KEY = "approval-key-" + "q" * 40  # pragma: allowlist secret
B32 = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret


def mk(**kw: Any) -> ApprovalSettings:
    return ApprovalSettings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_defaults_are_off() -> None:
    s = mk()
    assert not s.enabled and s.totp_bytes() is None and s.secret_values() == ()
    with pytest.raises(RuntimeError):
        s.key_bytes()
    assert s.ttl_s == 600


@pytest.mark.parametrize(
    ("field", "value"),
    [("hmac_key", "short-key-" + "z" * 5), ("totp_secret", "not-base32!!" + "Q" * 20)],
)
def test_bad_secrets_are_refused_without_echo(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        mk(**{field: value})
    assert value not in str(exc.value)


def test_secrets_are_hidden_in_repr_and_listed_for_the_scrubber() -> None:
    s = mk(hmac_key=SecretStr(KEY), totp_secret=SecretStr(B32))
    assert KEY not in repr(s) and B32 not in repr(s)
    assert s.secret_values() == (KEY, B32)
    assert s.key_bytes() == KEY.encode() and len(s.totp_bytes() or b"") == 20


@pytest.mark.parametrize(
    ("field", "value"),
    [("ttl_s", 59), ("ttl_s", 901), ("max_open", 0), ("totp_attempts_per_action", 6)],
)
def test_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        mk(**{field: value})


def test_runtime_adds_approval_secrets_to_the_scrubber() -> None:
    ap = mk(hmac_key=SecretStr(KEY), totp_secret=SecretStr(B32))
    cfg = AppConfig.build(
        app=AppSettings(_env_file=None),  # type: ignore[call-arg]
        gateway=None,
        llm=LLMSettings(_env_file=None),  # type: ignore[call-arg]
        net=NetSettings(_env_file=None),  # type: ignore[call-arg]
        dsn="postgresql+asyncpg://u:p@h/db",  # pragma: allowlist secret
        approvals=ap,
    )
    assert KEY in cfg.secrets and B32 in cfg.secrets and KEY not in repr(cfg)


@pytest.mark.parametrize(
    ("approvals", "gateway"),
    [(None, True), ("off", True), ("on", False)],
)
def test_build_approvals_is_off_without_key_or_user(approvals: Any, gateway: bool) -> None:
    ap = {None: None, "off": mk(), "on": mk(hmac_key=SecretStr(KEY))}[approvals]
    cfg = AppConfig.build(
        app=AppSettings(_env_file=None),  # type: ignore[call-arg]
        gateway=make_settings() if gateway else None,
        llm=LLMSettings(_env_file=None),  # type: ignore[call-arg]
        net=NetSettings(_env_file=None),  # type: ignore[call-arg]
        dsn="postgresql+asyncpg://u:p@h/db",  # pragma: allowlist secret
        approvals=ap,
    )
    assert build_approvals(cfg, object(), InMemoryGateway(), object()) is None  # type: ignore[arg-type]


def test_build_approvals_on_with_key_and_user() -> None:
    cfg = AppConfig.build(
        app=AppSettings(_env_file=None),  # type: ignore[call-arg]
        gateway=make_settings(),
        llm=LLMSettings(_env_file=None),  # type: ignore[call-arg]
        net=NetSettings(_env_file=None),  # type: ignore[call-arg]
        dsn="postgresql+asyncpg://u:p@h/db",  # pragma: allowlist secret
        approvals=mk(hmac_key=SecretStr(KEY)),
    )
    from src.core.clock import SystemClock

    assert build_approvals(cfg, object(), InMemoryGateway(), SystemClock()) is not None  # type: ignore[arg-type]


# ------------------------------------------------------------------- gateway


def test_callback_carries_the_sender_and_hides_data_in_repr() -> None:
    raw = {
        "update_id": 5,
        "callback_query": {
            "id": "cb",
            "from": {"id": USER},
            "data": "ap:secret-token-body",
            "message": {"message_id": 9, "chat": {"id": USER, "type": "private"}},
        },
    }
    parsed = parse_update(raw, USER)
    assert parsed is not None and parsed.update is not None
    assert parsed.update.from_user_id == USER
    assert "secret-token-body" not in repr(parsed.update)


async def test_send_keypad_builds_the_inline_keyboard() -> None:
    from src.gateway.telegram import TelegramGateway
    from tests.unit.gateway.fake_store import MemStore

    fake = FakeTelegram()
    gw = TelegramGateway(
        make_settings(per_chat_interval_s=0, global_interval_s=0), make_egress(fake), MemStore()
    )
    rows = [[("1", "tp:ab:1"), ("<b>", "tp:ab:2")]]
    await gw.send_keypad("Enter <code>", rows)
    (body,) = fake.sent
    assert body["text"] == "Enter &lt;code&gt;"
    assert body["reply_markup"]["inline_keyboard"] == [
        [{"text": "1", "callback_data": "tp:ab:1"}, {"text": "<b>", "callback_data": "tp:ab:2"}]
    ]
    assert body["link_preview_options"] == {"is_disabled": True}
    with pytest.raises(ValueError, match="64"):
        await gw.send_keypad("x", [[("1", "x" * 65)]])
    with pytest.raises(MessageTooLong):
        await gw.send_keypad("x" * 5000, rows)


# --------------------------------------------------------------- turn worker


class Handler:
    def __init__(self) -> None:
        self.seen: list[InboundUpdate] = []

    async def handle_callback(self, update: InboundUpdate) -> None:
        self.seen.append(update)


class NoModel:
    calls = 0

    async def run_turn(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - must not run
        NoModel.calls += 1
        raise AssertionError("callbacks never reach the model")


async def _serve_one(worker: TurnWorker, q: asyncio.Queue[PendingTurn], item: PendingTurn) -> None:
    q.put_nowait(item)
    task = asyncio.ensure_future(worker.serve())
    await q.join()
    task.cancel()


@pytest.mark.parametrize("wired", [True, False])
async def test_callbacks_go_to_the_approvals_handler_never_the_model(wired: bool) -> None:
    gw = InMemoryGateway()
    q: asyncio.Queue[PendingTurn] = asyncio.Queue()
    handler = Handler()
    worker = TurnWorker(
        gw,
        NoModel(),
        q,
        history_messages=4,
        callbacks=handler if wired else None,  # type: ignore[arg-type]
    )
    cb = InboundUpdate(
        1, UpdateKind.CALLBACK, callback_id="c1", callback_data="ap:x", from_user_id=USER
    )
    await _serve_one(worker, q, PendingTurn(cb, None))
    assert NoModel.calls == 0
    if wired:
        assert handler.seen == [cb] and gw.callbacks == []
    else:
        assert gw.callbacks == [("c1", STALE_CALLBACK_MESSAGE)]
