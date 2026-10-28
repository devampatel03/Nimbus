# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.2 approval flow on real Postgres: request -> card -> callback -> verify ->
(step-up) -> atomic consume -> execute. Table-driven attacks; the pass
condition everywhere is "the spy tool did not run" unless the user's genuine,
in-time, single tap (plus TOTP where demanded) was verified and consumed."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.approvals import service as svc
from src.approvals import token as tok
from src.approvals.types import AskOutcome
from src.gateway.types import MessageTooLong, UpdateKind
from tests._approval_helpers import (
    OTHER_USER,
    USER,
    Harness,
    build_harness,
    isolated_schema,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with isolated_schema() as (_, factory):
        yield factory


@pytest.fixture
def h(sf: async_sessionmaker[AsyncSession]) -> Harness:
    return build_harness(sf)


ARGS = {"task_id": "t-1", "title": "pay rent"}


async def requested(h: Harness, tool: str = "tasks_update", args: Any = None) -> Any:
    res = await h.propose(tool, ARGS if args is None else args)
    assert res.status == "denied" and res.needs_approval and res.approval == "requested"
    assert h.tools[tool].runs == 0
    return h.last_card()


# --------------------------------------------------------------- happy path


async def test_tainted_write_runs_once_after_genuine_tap_and_never_on_replay(h: Harness) -> None:
    card = await requested(h)
    assert '"title": "pay rent"' in card.text and "Tool: tasks_update" in card.text
    aid = await h.only_action_id()
    assert await h.status(aid) == "pending"

    assert await h.tap(card.approve_data) == "Approved. Running it now."
    assert h.tools["tasks_update"].calls == [ARGS]
    assert await h.status(aid) == "consumed"

    # Replays (Telegram redelivery, double tap, the reject button afterwards).
    for data in (card.approve_data, card.approve_data, card.reject_data):
        assert await h.tap(data) == svc.DONE_ALREADY
    assert h.tools["tasks_update"].runs == 1


async def test_reject_closes_the_row_and_a_later_approve_does_nothing(h: Harness) -> None:
    card = await requested(h)
    aid = await h.only_action_id()
    assert await h.tap(card.reject_data) == svc.REJECTED
    assert await h.status(aid) == "rejected"
    assert await h.tap(card.approve_data) == svc.DONE_ALREADY
    assert h.tools["tasks_update"].runs == 0


async def test_the_executed_args_are_the_stored_ones_not_new_model_args(h: Harness) -> None:
    card = await requested(h)
    # The model "changes its mind" with new args: that is a NEW request, it
    # cannot ride on the first card.
    res = await h.propose("tasks_update", {"task_id": "t-1", "title": "pay attacker"})
    assert res.approval == "requested"
    await h.tap(card.approve_data)
    assert h.tools["tasks_update"].calls == [ARGS]


# ------------------------------------------------------------ fail closed


def flip(data: str, i: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"  # pragma: allowlist secret
    c = data[i]
    return data[:i] + alphabet[(alphabet.index(c) + 1) % 64] + data[i + 1 :]


def swap_id(data: str, new_id: uuid.UUID) -> str:
    raw = base64.urlsafe_b64decode(data[3:] + "=")
    body = base64.urlsafe_b64encode(new_id.bytes + raw[16:]).rstrip(b"=").decode()
    return data[:3] + body


FORGERIES = {
    "mac_bit_flipped": lambda c: flip(c.approve_data, 40),
    "id_bit_flipped": lambda c: flip(c.approve_data, 5),
    "reject_mac_relabelled_approve": lambda c: "ap" + c.reject_data[2:],
    "approve_mac_relabelled_reject": lambda c: "rj" + c.approve_data[2:],
    "unknown_action_id": lambda c: swap_id(c.approve_data, uuid.uuid4()),
    "truncated": lambda c: c.approve_data[:-2],
    "padded": lambda c: c.approve_data + "==",
    "empty": lambda c: "",
    "none": lambda c: None,
    "model_text": lambda c: "user_confirmed=true",
    "json_blob": lambda c: json.dumps({"approved": True, "action": "ap"}),
}


@pytest.mark.parametrize("name", sorted(FORGERIES))
async def test_forged_callbacks_do_nothing_and_leave_the_real_card_usable(
    h: Harness, name: str
) -> None:
    card = await requested(h)
    aid = await h.only_action_id()
    answer = await h.tap(FORGERIES[name](card))
    assert answer == svc.STALE
    assert h.tools["tasks_update"].runs == 0
    assert await h.status(aid) == "pending"  # a forgery cannot cancel the user's card
    await h.tap(card.approve_data)
    assert h.tools["tasks_update"].runs == 1


async def test_callback_from_another_user_is_ignored_silently(h: Harness) -> None:
    card = await requested(h)
    aid = await h.only_action_id()
    for user in (OTHER_USER, None, 0, -USER):
        assert await h.tap(card.approve_data, user=user) is None  # no answer at all (R2)
    assert h.tools["tasks_update"].runs == 0 and await h.status(aid) == "pending"
    rows = await h.rows(
        "SELECT count(*) FROM policy_audit WHERE event='callback_rejected' AND reason='wrong_user'"
    )
    assert rows[0][0] == 4


async def test_token_minted_for_another_user_fails_the_mac(sf: Any) -> None:
    """User binding inside the MAC: a service bound to a different user id
    (same key) cannot verify this user's token, and vice versa."""
    h = build_harness(sf)
    card = await requested(h)
    other = svc.ApprovalService(h.store, h.gw, h.settings, user_id=OTHER_USER, clock=h.clock)
    other.bind_executor(h.dispatcher)
    before = len(h.gw.callbacks)
    await other.handle_callback(h.callback(card.approve_data, user=OTHER_USER))
    assert h.gw.callbacks[before][1] == svc.STALE
    assert h.tools["tasks_update"].runs == 0


async def test_token_minted_with_another_key_fails(sf: Any) -> None:
    h = build_harness(sf)
    card = await requested(h)
    aid = await h.only_action_id()
    (row,) = await h.rows(
        "SELECT payload_sha256, nonce, expires_at FROM pending_action WHERE id=:i", i=aid
    )
    forged = tok.encode(
        tok.Verb.APPROVE,
        aid,
        tok.compute_mac(
            b"attacker-key-" + b"z" * 32,
            verb=tok.Verb.APPROVE,
            action_id=aid,
            payload_sha256_hex=row[0],
            nonce=bytes(row[1]),
            expiry_epoch_s=int(row[2].timestamp()),
            user_id=USER,
        ),
    )
    assert forged != card.approve_data
    assert await h.tap(forged) == svc.STALE
    assert h.tools["tasks_update"].runs == 0


@pytest.mark.parametrize("late_s", [600, 601, 3600])
async def test_expired_approval_never_runs(h: Harness, late_s: int) -> None:
    card = await requested(h)
    aid = await h.only_action_id()
    h.clock.advance(late_s)
    assert await h.tap(card.approve_data) == svc.EXPIRED
    assert await h.status(aid) == "expired"
    assert await h.tap(card.approve_data) == svc.DONE_ALREADY
    assert h.tools["tasks_update"].runs == 0


async def test_just_before_expiry_still_runs(h: Harness) -> None:
    card = await requested(h)
    h.clock.advance(599)
    await h.tap(card.approve_data)
    assert h.tools["tasks_update"].runs == 1


async def test_row_whose_payload_does_not_match_its_hash_never_runs(h: Harness) -> None:
    """A DB-level attacker who bypassed the append-only trigger and planted a
    row (or edited one) with a payload that does not hash to the MACed hash."""
    real_sha = hashlib.sha256(b"something else").hexdigest()
    aid = uuid.uuid4()
    nonce = b"n" * 16
    exp = h.clock.now().replace(microsecond=0) + timedelta(minutes=5)
    async with h.sf() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO pending_action (id, tool, action, payload, payload_sha256, nonce,"
                " requires_totp, status, created_at, expires_at) VALUES (:i, 'tasks_update',"
                " 'call', CAST(:p AS jsonb), :h, :n, false, 'pending', :c, :e)"
            ),
            {
                "i": aid,
                "p": json.dumps(ARGS),
                "h": real_sha,
                "n": nonce,
                "c": h.clock.now(),
                "e": exp,
            },
        )
    mac = h.service._mac(tok.Verb.APPROVE, aid, real_sha, nonce, exp)
    assert await h.tap(tok.encode(tok.Verb.APPROVE, aid, mac)) == svc.STALE
    assert h.tools["tasks_update"].runs == 0
    rows = await h.rows(
        "SELECT count(*) FROM policy_audit WHERE reason='payload_hash_mismatch' AND action_id=:i",
        i=aid,
    )
    assert rows[0][0] == 1


async def test_payload_cannot_be_edited_in_place(h: Harness) -> None:
    await requested(h)
    aid = await h.only_action_id()
    with pytest.raises(DBAPIError, match="immutable column"):
        async with h.sf() as s, s.begin():
            await s.execute(
                text("UPDATE pending_action SET payload = '{\"x\": 1}'::jsonb WHERE id=:i"),
                {"i": aid},
            )


async def test_concurrent_consumes_have_exactly_one_winner(h: Harness) -> None:
    await requested(h)
    aid = await h.only_action_id()
    (row,) = await h.rows("SELECT payload_sha256 FROM pending_action WHERE id=:i", i=aid)
    results = await asyncio.gather(
        *(
            h.store.consume(aid, payload_sha256=row[0], now=h.clock.now(), step_up_verified=False)
            for _ in range(8)
        ),
        return_exceptions=True,
    )
    winners = [r for r in results if r is not None and not isinstance(r, BaseException)]
    assert len(winners) == 1


async def test_second_consume_is_refused_cleanly(h: Harness) -> None:
    """The store's own `status = 'pending'` condition (not only the trigger)
    makes a replayed consume a clean None, never an exception."""
    await requested(h)
    aid = await h.only_action_id()
    (row,) = await h.rows("SELECT payload_sha256 FROM pending_action WHERE id=:i", i=aid)
    kw: dict[str, Any] = {"payload_sha256": row[0], "now": h.clock.now(), "step_up_verified": False}
    assert await h.store.consume(aid, **kw) is not None
    assert await h.store.consume(aid, **kw) is None
    assert await h.store.close(aid, "rejected") is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("sha", "0" * 64), ("now_late", None), ("step_up_missing", None)],
)
async def test_consume_rechecks_everything_in_one_statement(
    h: Harness, field: str, value: Any
) -> None:
    await h.propose("tasks_delete", {"task_id": "x"})  # requires_totp row
    aid = await h.only_action_id()
    (row,) = await h.rows("SELECT payload_sha256 FROM pending_action WHERE id=:i", i=aid)
    sha, now, su = row[0], h.clock.now(), True
    if field == "sha":
        sha = value
    elif field == "now_late":
        now = now + timedelta(hours=1)
    else:
        su = False
    assert await h.store.consume(aid, payload_sha256=sha, now=now, step_up_verified=su) is None
    assert await h.status(aid) == "pending"
    ok = await h.store.consume(aid, payload_sha256=row[0], now=h.clock.now(), step_up_verified=True)
    assert ok is not None and ok.step_up_verified is True


# --------------------------------------------------------- request bounds


async def test_identical_open_request_is_not_carded_twice(h: Harness) -> None:
    await requested(h)
    res = await h.propose("tasks_update", ARGS)
    assert res.approval == "duplicate" and len(h.gw.cards) == 1


async def test_open_card_cap(h: Harness) -> None:
    for i in range(h.settings.max_open):
        await requested(h, args={"task_id": f"t{i}"})
    res = await h.propose("tasks_update", {"task_id": "one-too-many"})
    assert res.approval == "too_many" and h.tools["tasks_update"].runs == 0
    h.clock.advance(h.settings.ttl_s + 1)  # expired rows stop counting
    await requested(h, args={"task_id": "later"})


async def test_unstorable_args_are_refused(h: Harness) -> None:
    res = await h.propose("tasks_update", {"title": "x" * 5000})
    assert res.approval == "invalid" and h.gw.cards == []


async def test_card_send_failure_cancels_the_row(h: Harness, monkeypatch: Any) -> None:
    async def boom(card: Any) -> Any:
        raise MessageTooLong("approval_payload_too_long")

    monkeypatch.setattr(h.gw, "send_approval", boom)
    res = await h.propose("tasks_update", ARGS)
    assert res.approval == "failed"
    aid = await h.only_action_id()
    assert await h.status(aid) == "cancelled"


async def test_untainted_internal_write_runs_without_a_card(h: Harness) -> None:
    res = await h.propose("tasks_update", ARGS, tainted=False)
    assert res.status == "ok" and h.gw.cards == []


# ---------------------------------------------------------------- step-up


async def stepup_card(h: Harness, task: str = "t-9") -> tuple[Any, uuid.UUID]:
    res = await h.propose("tasks_delete", {"task_id": task})
    assert res.approval == "requested" and res.step_up
    card = h.last_card()
    assert "authenticator code" in card.text
    return card, await h.only_action_id()


async def test_destructive_runs_only_after_correct_totp(h: Harness) -> None:
    card, aid = await stepup_card(h)
    assert await h.tap(card.approve_data) == svc.ENTER_CODE
    assert h.tools["tasks_delete"].runs == 0 and await h.status(aid) == "pending"
    assert await h.enter_code(h.current_code()) == "Approved. Running it now."
    assert h.tools["tasks_delete"].calls == [{"task_id": "t-9"}]
    assert await h.status(aid) == "consumed"


@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_totp_window_accepts_adjacent_steps(h: Harness, offset: int) -> None:
    card, _ = await stepup_card(h)
    await h.tap(card.approve_data)
    await h.enter_code(h.current_code(offset))
    assert h.tools["tasks_delete"].runs == 1


async def test_wrong_codes_reject_the_action_after_the_attempt_budget(h: Harness) -> None:
    card, aid = await stepup_card(h)
    await h.tap(card.approve_data)
    for _ in range(h.settings.totp_attempts_per_action - 1):
        assert (await h.enter_code(h.wrong_code()) or "").startswith(svc.WRONG_CODE)
        assert await h.status(aid) == "pending"
    await h.enter_code(h.wrong_code())
    assert await h.status(aid) == "rejected" and h.tools["tasks_delete"].runs == 0
    assert await h.tap(card.approve_data) == svc.DONE_ALREADY


async def test_code_replay_across_actions_is_refused(h: Harness) -> None:
    c1, _ = await stepup_card(h, "a")
    await h.tap(c1.approve_data)
    code = h.current_code()
    await h.enter_code(code)
    assert h.tools["tasks_delete"].runs == 1
    c2, aid2 = await stepup_card(h, "b")
    await h.tap(c2.approve_data)
    assert (await h.enter_code(code) or "").startswith(svc.WRONG_CODE)
    assert h.tools["tasks_delete"].runs == 1 and await h.status(aid2) == "pending"
    h.clock.advance(30)  # next step: a fresh code works
    await h.enter_code(h.current_code())
    assert h.tools["tasks_delete"].runs == 2


async def test_lockout_after_failures_blocks_even_a_correct_code(h: Harness) -> None:
    failures = 0
    while failures < h.settings.totp_lockout_failures:
        card, _ = await stepup_card(h, f"x{failures}")
        await h.tap(card.approve_data)
        for _ in range(h.settings.totp_attempts_per_action):
            if failures >= h.settings.totp_lockout_failures:
                break
            await h.enter_code(h.wrong_code())
            failures += 1
    card, aid = await stepup_card(h, "victim")
    assert await h.tap(card.approve_data) == svc.LOCKED
    assert h.tools["tasks_delete"].runs == 0 and await h.status(aid) == "pending"
    # The lockout is durable (policy_audit), not process memory: a new service
    # (a restart) is still locked.
    fresh = svc.ApprovalService(h.store, h.gw, h.settings, user_id=USER, clock=h.clock)
    before = len(h.gw.callbacks)
    await fresh.handle_callback(h.callback(card.approve_data))
    assert h.gw.callbacks[before][1] == svc.LOCKED
    h.clock.advance(h.settings.totp_lockout_window_s + 1)
    h.clock.advance(0)
    card2, _ = await stepup_card(h, "after-lockout")
    assert await h.tap(card2.approve_data) == svc.ENTER_CODE


async def test_keypad_cancel_backspace_stale_and_expiry(h: Harness) -> None:
    card, _aid = await stepup_card(h)
    await h.tap(card.approve_data)
    good = h.current_code()
    await h.tap(h.keypad_data("1"))
    assert await h.tap(h.keypad_data("b")) == "0/6"
    assert await h.tap("tp:deadbeef:1") == svc.STALE  # wrong challenge tag
    assert await h.tap(h.keypad_data("1")[:-1] + "x") == svc.STALE  # unknown key
    await h.enter_code(good)
    assert h.tools["tasks_delete"].runs == 1

    card2, aid2 = await stepup_card(h, "t-10")
    await h.tap(card2.approve_data)
    assert await h.tap(h.keypad_data("c")) == svc.CANCELLED
    assert await h.status(aid2) == "cancelled"
    assert await h.tap(h.keypad_data("1")) == svc.STALE

    card3, aid3 = await stepup_card(h, "t-11")
    await h.tap(card3.approve_data)
    h.clock.advance(h.settings.step_up_window_s)
    assert await h.tap(h.keypad_data("1")) == svc.EXPIRED
    assert await h.status(aid3) == "expired"
    assert h.tools["tasks_delete"].runs == 1


async def test_step_up_without_a_totp_secret_is_unavailable(sf: Any) -> None:
    h = build_harness(sf, totp_on=False)
    res = await h.propose("tasks_delete", {"task_id": "x"})
    assert res.approval == "unavailable" and h.gw.cards == []
    assert await h.rows("SELECT id FROM pending_action") == []
    # Non step-up approvals still work without TOTP.
    card = await requested(h)
    await h.tap(card.approve_data)
    assert h.tools["tasks_update"].runs == 1


async def test_policy_still_decides_after_consume(sf: Any) -> None:
    """A consumed approval is not a bypass: the kill switch (or any deny row)
    applies when the approved call is executed."""
    h = build_harness(sf)
    card = await requested(h)

    async def killed() -> str:
        return "killed"

    h.dispatcher._mode_source = killed
    await h.tap(card.approve_data)
    assert h.tools["tasks_update"].runs == 0
    rows = await h.rows(
        "SELECT rule_id FROM policy_audit WHERE kind='decision' AND event='decided_confirmed'"
    )
    assert rows == [("deny.killed",)]


# ------------------------------------------------------------------ audit


async def test_every_decision_and_approval_event_is_audited_without_values(h: Harness) -> None:
    canary = "CANARY-p32-audit-91d3"
    await h.propose("tasks_list", {"q": canary}, tainted=False)
    res = await h.propose("tasks_update", {"task_id": canary})
    assert res.approval == "requested"
    await h.tap(h.last_card().approve_data)
    rows = await h.rows(
        "SELECT kind, event, tool, rule_id, outcome, executed FROM policy_audit ORDER BY created_at"
    )
    events = [(r[0], r[1]) for r in rows]
    assert ("decision", "decided") in events and ("approval", "requested") in events
    assert ("approval", "consumed") in events and ("approval", "executed") in events
    assert ("decision", "decided_confirmed") in events
    dump = json.dumps([list(map(str, r)) for r in await h.rows("SELECT * FROM policy_audit")])
    assert canary not in dump and h.last_card().approve_data[3:] not in dump


async def test_audit_rows_are_append_only(h: Harness) -> None:
    await h.propose("tasks_list", {}, tainted=False)
    for sql in ("UPDATE policy_audit SET event='x'", "DELETE FROM policy_audit"):
        with pytest.raises(DBAPIError):
            async with h.sf() as s, s.begin():
                await s.execute(text(sql))


async def test_non_callback_update_is_ignored(h: Harness) -> None:
    from src.gateway.types import InboundUpdate

    await h.service.handle_callback(
        InboundUpdate(1, UpdateKind.TEXT, text="ap:x", from_user_id=USER)
    )
    assert h.gw.callbacks == []


async def test_promotion_request_payload_shape(h: Harness) -> None:
    item = uuid.uuid4()
    assert await h.service.request_memory_promotion(item, preview="hello") is AskOutcome.REQUESTED
    (row,) = await h.rows("SELECT tool, action, payload, requires_totp FROM pending_action")
    assert (row[0], row[1], row[2], row[3]) == ("memory", "promote", {"item_id": str(item)}, False)
    assert await h.service.request_memory_promotion("nope") is AskOutcome.INVALID  # type: ignore[arg-type]


async def test_memory_promotion_end_to_end_with_the_real_store(h: Harness) -> None:
    """R8: the approval the service writes is exactly what
    `PgVectorMemoryStore.promote` accepts, once."""
    from src.app.runtime import memory_promoter
    from src.memory.store import ChunkIn
    from tests._memory_helpers import EMB, make_event, store_for

    async with h.sf() as s, s.begin():
        store = store_for(s)
        ev = await store.record_event(make_event(lane="untrusted", kind="recorder_transcript"))  # type: ignore[arg-type]
        (chunk,) = await store.add_chunks(ev, [ChunkIn(text="maybe true")])
    h.service.bind_promoter(memory_promoter(h.sf, EMB))
    assert await h.service.request_memory_promotion(chunk) is AskOutcome.REQUESTED
    card = h.last_card()
    await h.tap(card.approve_data)
    (row,) = await h.rows(
        "SELECT promoted, promoted_approval_id FROM memory_chunk WHERE id=:i", i=chunk
    )
    assert row[0] is True and row[1] == await h.only_action_id()
    assert await h.tap(card.approve_data) == svc.DONE_ALREADY
    audit = await h.rows("SELECT event, outcome FROM policy_audit WHERE event='executed'")
    assert audit == [("executed", "ok")]


async def test_memory_promotion_refused_by_store_is_reported_not_retried(h: Harness) -> None:
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def refuse(item: uuid.UUID, approval_id: uuid.UUID) -> None:
        calls.append((item, approval_id))
        raise RuntimeError("promotion_refused")

    h.service.bind_promoter(refuse)
    item = uuid.uuid4()
    await h.service.request_memory_promotion(item)
    await h.tap(h.last_card().approve_data)
    assert len(calls) == 1 and calls[0][0] == item
    audit = await h.rows("SELECT outcome FROM policy_audit WHERE event='exec_failed'")
    assert audit == [("error",)]
