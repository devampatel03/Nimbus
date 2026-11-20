# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P3.2 failure paths: storage / Telegram failures, configuration gaps and
races between the card, the keypad and the row. Every one must end with the
tool NOT run (fail closed) and the process still serving."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.approvals import service as svc
from src.approvals.store import ApprovalStore
from src.approvals.types import AskOutcome
from tests._approval_helpers import USER, Harness, approval_settings, build_harness, isolated_schema


@pytest.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with isolated_schema() as (_, factory):
        yield factory


@pytest.fixture
def h(sf: async_sessionmaker[AsyncSession]) -> Harness:
    return build_harness(sf)


def boom(*a: Any, **k: Any) -> Any:
    raise ConnectionError("db down")


async def aboom(*a: Any, **k: Any) -> Any:
    raise ConnectionError("db down")


def test_service_refuses_bad_configuration(sf: Any) -> None:
    store = ApprovalStore(sf)
    from pydantic import SecretStr

    from src.approvals.settings import ApprovalSettings

    off = ApprovalSettings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="HMAC"):
        svc.ApprovalService(store, object(), off, user_id=USER)  # type: ignore[arg-type]
    on = ApprovalSettings(_env_file=None, hmac_key=SecretStr("k" * 32))  # type: ignore[call-arg]
    for bad in (0, -1, True, "1"):
        with pytest.raises(ValueError, match="user id"):
            svc.ApprovalService(store, object(), on, user_id=bad)  # type: ignore[arg-type]


async def test_storage_failure_during_request_is_a_failed_outcome(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(h.store, "open_duplicate", aboom)
    res = await h.propose("tasks_update", {"task_id": "1"})
    assert res.approval == "failed" and h.gw.cards == []
    assert await h.service.request_memory_promotion(uuid.uuid4()) is AskOutcome.FAILED


async def test_storage_failure_during_callback_answers_and_runs_nothing(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.propose("tasks_update", {"task_id": "1"})
    card = h.last_card()
    monkeypatch.setattr(h.store, "load", aboom)
    assert await h.tap(card.approve_data) == svc.FAILED
    assert h.tools["tasks_update"].runs == 0


async def test_telegram_failures_on_answer_and_close_are_tolerated(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.propose("tasks_update", {"task_id": "1"})
    card = h.last_card()
    monkeypatch.setattr(h.gw, "answer_callback", aboom)
    monkeypatch.setattr(h.gw, "close_approval", aboom)
    await h.service.handle_callback(h.callback(card.approve_data))
    assert h.tools["tasks_update"].runs == 1


async def test_callback_without_id_or_message_still_verifies(h: Harness) -> None:
    await h.propose("tasks_update", {"task_id": "1"})
    upd = h.callback(h.last_card().approve_data, message_id=None)
    object.__setattr__(upd, "callback_id", None)
    await h.service.handle_callback(upd)
    assert h.tools["tasks_update"].runs == 1 and h.gw.callbacks == []


async def test_audit_failure_on_a_wrong_user_callback_is_contained(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(h.store, "audit", aboom)
    await h.service.handle_callback(h.callback("ap:x", user=1))
    assert h.gw.callbacks == []


async def test_row_needing_step_up_when_totp_was_removed_is_rejected(sf: Any) -> None:
    h = build_harness(sf)
    await h.propose("tasks_delete", {"task_id": "1"})
    card = h.last_card()
    no_totp = svc.ApprovalService(
        h.store, h.gw, approval_settings(totp_on=False), user_id=USER, clock=h.clock
    )
    await no_totp.handle_callback(h.callback(card.approve_data))
    assert h.gw.callbacks[-1][1] == svc.NOT_CONFIGURED
    assert await h.status(await h.only_action_id()) == "rejected"
    assert h.tools["tasks_delete"].runs == 0


async def test_lockout_reached_while_typing_blocks_the_code(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.propose("tasks_delete", {"task_id": "1"})
    await h.tap(h.last_card().approve_data)
    code = h.current_code()

    async def many(*a: Any, **k: Any) -> int:
        return 99

    monkeypatch.setattr(h.store, "totp_failures_since", many)
    assert await h.enter_code(code) == svc.LOCKED
    assert h.tools["tasks_delete"].runs == 0


async def test_rejecting_the_card_while_the_keypad_is_open_wins(h: Harness) -> None:
    await h.propose("tasks_delete", {"task_id": "1"})
    card = h.last_card()
    await h.tap(card.approve_data)
    code = h.current_code()
    await h.tap(card.reject_data)
    assert await h.enter_code(code) == svc.STALE
    assert h.tools["tasks_delete"].runs == 0


async def test_reject_that_loses_the_race_to_approve_notifies_nothing(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3.9 LOW-A: Approve consumes the row between Reject's load and close.
    The listener must not hear `rejected` for an action that was approved."""
    await h.propose("tasks_update", {"task_id": "1"})
    card = h.last_card()
    heard: list[str] = []

    async def listener(tool: str, action: str, payload: Any, outcome: str) -> None:
        heard.append(outcome)

    h.service.bind_listener(listener)
    real_close = h.store.close

    async def close_after_approve(aid: uuid.UUID, status: str) -> bool:
        monkeypatch.setattr(h.store, "close", real_close)
        await h.tap(card.approve_data)  # the concurrent Approve wins
        return await real_close(aid, status)

    monkeypatch.setattr(h.store, "close", close_after_approve)
    assert await h.tap(card.reject_data) == svc.DONE_ALREADY
    assert h.tools["tasks_update"].runs == 1
    assert "rejected" not in heard
    assert await h.status(await h.only_action_id()) == "consumed"


async def test_expiry_that_loses_the_race_to_reject_notifies_nothing(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late tap finds the row expired, but a concurrent Reject closes it
    first: the late tap must not report or notify `expired` for it."""
    await h.propose("tasks_update", {"task_id": "1"})
    card = h.last_card()
    heard: list[str] = []

    async def listener(tool: str, action: str, payload: Any, outcome: str) -> None:
        heard.append(outcome)

    h.service.bind_listener(listener)
    h.clock.advance(h.settings.ttl_s + 1)
    real_close = h.store.close

    async def close_after_other(aid: uuid.UUID, status: str) -> bool:
        assert status == "expired"
        assert await real_close(aid, "rejected")  # the other tap won
        return await real_close(aid, status)

    monkeypatch.setattr(h.store, "close", close_after_other)
    assert await h.tap(card.approve_data) == svc.DONE_ALREADY
    assert heard == []
    assert await h.status(await h.only_action_id()) == "rejected"
    assert h.tools["tasks_update"].runs == 0


async def test_consume_refused_at_the_last_moment_runs_nothing(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.propose("tasks_update", {"task_id": "1"})

    async def refuse(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(h.store, "consume", refuse)
    assert await h.tap(h.last_card().approve_data) == svc.STALE
    assert h.tools["tasks_update"].runs == 0


async def test_without_an_executor_the_consumed_call_is_not_run_and_is_audited(sf: Any) -> None:
    h = build_harness(sf)
    lonely = svc.ApprovalService(h.store, h.gw, h.settings, user_id=USER, clock=h.clock)
    h.dispatcher._approvals = lonely
    await h.propose("tasks_update", {"task_id": "1"})
    await lonely.handle_callback(h.callback(h.last_card().approve_data))
    assert h.tools["tasks_update"].runs == 0
    rows = await h.rows("SELECT rule_id FROM policy_audit WHERE event='exec_failed'")
    assert rows == [("exec.no_executor",)]


async def test_card_failure_audit_error_is_contained(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(h.gw, "send_approval", aboom)
    real_audit = h.store.audit

    async def picky(rec: Any, **k: Any) -> None:
        if rec.event == "card_failed":
            raise ConnectionError("x")
        await real_audit(rec, **k)

    monkeypatch.setattr(h.store, "audit", picky)
    res = await h.propose("tasks_update", {"task_id": "1"})
    assert res.approval == "failed"
