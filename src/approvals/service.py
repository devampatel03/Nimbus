# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Approval flow (ADR-0009, INV-5, R8): request -> card -> callback -> verify ->
optional TOTP step-up -> atomic consume -> execute.

Request (`request`, called by the dispatcher on `require_approval`): the
model-proposed call is copied to plain JSON, hashed (tool, action, canonical
args), stored in `pending_action` with a random 16-byte nonce and a 10-minute
expiry, and shown on a card built by code from the stored payload (never from
model prose). Bounded: identical open payloads are not re-carded and at most
`max_open` cards are open at once.

Callback (`handle_callback`, called by the turn worker for CALLBACK updates):

1. `from_user_id` must be the allowlisted user (the gateway already drops
   anyone else; this is the second check, and the MAC binds the user id too).
2. Strict token parse; the row must exist.
3. The stored payload is re-hashed and must equal the stored hash (a row edited
   in the database fails), then the MAC is recomputed from the stored row and
   compared in constant time. A bad MAC changes nothing (an attacker cannot
   cancel the user's real card).
4. Not pending -> "already handled" (replay). Expired -> marked expired.
5. Reject -> marked rejected. Approve -> if the row needs a step-up, a keypad
   challenge starts (digits arrive as callbacks, so the code is never a
   message, never an event, never memory); else consume.
6. `ApprovalStore.consume` is the atomic `pending -> consumed` UPDATE,
   committed BEFORE the executor runs anything. Only it mints the
   `ConsumedApproval` that lets the dispatcher pass `user_confirmed=True`.

TOTP: +-1 step, a code is never accepted twice (last accepted step is in
`policy_audit`), `totp_attempts_per_action` wrong codes for one action (counted
durably per action id, across Approve taps and restarts) reject it, and
`totp_lockout_failures` wrong codes in `totp_lockout_window_s` lock step-up
(counted durably in `policy_audit`, so a restart does not reset it).

Never logged: tokens, MACs, codes, digits, argument values (INV-7). Log lines
carry fixed event names and the first 8 hex chars of the action id only.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from src.approvals import token as tok
from src.approvals import totp
from src.approvals.canonical import PayloadError, payload_sha256, plain_payload
from src.approvals.card import render_card, render_step_up_prompt, reveal
from src.approvals.settings import ApprovalSettings
from src.approvals.store import TOTP_FAILED, TOTP_OK, ApprovalStore, PendingRow
from src.approvals.types import (
    COMMITMENT_TOOL,
    CONFIRM_ACTION,
    MEMORY_TOOL,
    PROMOTE_ACTION,
    TOOL_CALL_ACTION,
    ApprovalAsk,
    AskOutcome,
    AuditRecord,
    ConsumedApproval,
)
from src.core.clock import Clock, SystemClock
from src.core.turn_context import current_event_id, turn_event
from src.gateway.types import ApprovalCard, InboundUpdate, MessageRef, UpdateKind

log = logging.getLogger(__name__)

STALE = "That action is no longer available."
DONE_ALREADY = "That action was already handled."
EXPIRED = "That approval expired. Ask again if you still want it."
REJECTED = "Rejected. Nothing was done."
LOCKED = "Too many wrong codes. Step-up is locked for a while; nothing was done."
NOT_CONFIGURED = "Step-up is not configured, so this action cannot be approved."
ENTER_CODE = "Enter your authenticator code on the keypad."
WRONG_CODE = "Wrong code."
CANCELLED = "Cancelled. Nothing was done."
FAILED = "Something went wrong; nothing was done."
KEYPAD_PREFIX = "tp:"
_MAX_PREVIEW = 300


class ApprovalGateway(Protocol):
    async def send_approval(self, card: ApprovalCard) -> MessageRef: ...

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None: ...

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...

    async def send_keypad(
        self, text: str, rows: Sequence[Sequence[tuple[str, str]]]
    ) -> MessageRef: ...


class ExecutionResult(Protocol):
    @property
    def status(self) -> str: ...

    @property
    def rule_id(self) -> str: ...


class ApprovalExecutor(Protocol):
    async def execute_approved(self, approval: ConsumedApproval) -> ExecutionResult: ...


# (item_id, approval_id) -> None; raises when the store refuses (R8).
Promoter = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]
# (candidate_id, approval_id) -> None; raises when the ledger refuses (P3.9).
Confirmer = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]
CardListener = Callable[[str, str, Mapping[str, Any], str], Awaitable[None]]


@dataclass
class _Challenge:
    tag: str
    action_id: uuid.UUID
    tool: str
    deadline: datetime
    keypad_ref: MessageRef | None
    card_ref: MessageRef | None
    digits: str = ""


def _short(action_id: uuid.UUID | None) -> str:
    return action_id.hex[:8] if action_id is not None else "-"


def keypad_rows(tag: str) -> list[list[tuple[str, str]]]:
    def b(label: str, key: str) -> tuple[str, str]:
        return (label, f"{KEYPAD_PREFIX}{tag}:{key}")

    return [
        [b("1", "1"), b("2", "2"), b("3", "3")],
        [b("4", "4"), b("5", "5"), b("6", "6")],
        [b("7", "7"), b("8", "8"), b("9", "9")],
        [b("Del", "b"), b("0", "0"), b("Cancel", "c")],
    ]


class ApprovalService:
    def __init__(
        self,
        store: ApprovalStore,
        gateway: ApprovalGateway,
        settings: ApprovalSettings,
        *,
        user_id: int,
        clock: Clock | None = None,
        executor: ApprovalExecutor | None = None,
        promoter: Promoter | None = None,
        confirmer: Confirmer | None = None,
    ) -> None:
        if not settings.enabled:
            raise ValueError("approvals need APPROVAL_HMAC_KEY")
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("user id must be a positive int")
        self._store = store
        self._gw = gateway
        self._s = settings
        self._key = settings.key_bytes()
        self._totp = settings.totp_bytes()
        self._user = user_id
        self._clock = clock or SystemClock()
        self._executor = executor
        self._promoter = promoter
        self._confirmer = confirmer
        self._listener: CardListener | None = None
        self._challenge: _Challenge | None = None

    def bind_executor(self, executor: ApprovalExecutor) -> None:
        self._executor = executor

    def bind_promoter(self, promoter: Promoter) -> None:
        self._promoter = promoter

    def bind_confirmer(self, confirmer: Confirmer) -> None:
        self._confirmer = confirmer

    def bind_listener(self, listener: CardListener) -> None:
        """Told when a card ends (`rejected`, `expired`, `done`). Decides nothing."""
        self._listener = listener

    async def _notify(
        self, tool: str, action: str, payload: Mapping[str, Any], outcome: str
    ) -> None:
        if self._listener is None:
            return
        try:
            await self._listener(tool, action, payload, outcome)
        except Exception as exc:  # noqa: BLE001 - bookkeeping only
            log.warning("card listener failed err=%s", type(exc).__name__)

    # ------------------------------------------------------------- helpers

    async def _audit(self, event: str, **kw: Any) -> None:
        await self._store.audit(AuditRecord(kind="approval", event=event, **kw), now=self._now())

    async def _audit_quiet(self, event: str, **kw: Any) -> None:
        try:
            await self._audit(event, **kw)
        except Exception as exc:  # noqa: BLE001 - an audit failure must not unblock anything
            log.warning("approval audit failed event=%s err=%s", event, type(exc).__name__)

    def _now(self) -> datetime:
        return self._clock.now()

    def _mac(
        self, verb: tok.Verb, row_id: uuid.UUID, sha: str, nonce: bytes, exp: datetime
    ) -> bytes:
        return tok.compute_mac(
            self._key,
            verb=verb,
            action_id=row_id,
            payload_sha256_hex=sha,
            nonce=nonce,
            expiry_epoch_s=int(exp.timestamp()),
            user_id=self._user,
        )

    # ------------------------------------------------------------- request

    async def request(self, ask: ApprovalAsk) -> AskOutcome:
        """Never raises: any failure is an outcome that the dispatcher reports
        as a denial (the call never runs from here)."""
        try:
            return await self._request(
                tool=ask.tool,
                action=TOOL_CALL_ACTION,
                args=ask.args,
                step_up=ask.step_up is not False,
                rule_id=ask.rule_id,
                tainted=ask.tainted is not False,
                preview=None,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed
            log.warning("approval request failed err=%s", type(exc).__name__)
            return AskOutcome.FAILED

    async def request_memory_promotion(
        self, item_id: uuid.UUID, *, preview: str | None = None
    ) -> AskOutcome:
        """R8: the approval that `PgVectorMemoryStore.promote` accepts: tool
        `memory`, action `promote`, payload exactly `{"item_id": "<uuid>"}`."""
        if not isinstance(item_id, uuid.UUID):
            return AskOutcome.INVALID
        try:
            return await self._request(
                tool=MEMORY_TOOL,
                action=PROMOTE_ACTION,
                args={"item_id": str(item_id)},
                step_up=False,
                rule_id="approval.memory_promote",
                tainted=True,
                preview=preview,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed
            log.warning("promotion request failed err=%s", type(exc).__name__)
            return AskOutcome.FAILED

    async def request_commitment_confirm(
        self, candidate_id: uuid.UUID, *, preview: str | None = None
    ) -> AskOutcome:
        """P3.9: ask the user to confirm ONE candidate commitment. Payload is exactly
        `{"candidate_id": "<uuid>"}`; the card text comes from the stored row (code),
        and only the user's genuine tap can consume it (INV-5, R8)."""
        if not isinstance(candidate_id, uuid.UUID):
            return AskOutcome.INVALID
        try:
            return await self._request(
                tool=COMMITMENT_TOOL,
                action=CONFIRM_ACTION,
                args={"candidate_id": str(candidate_id)},
                step_up=False,
                rule_id="approval.commitment_confirm",
                tainted=True,
                preview=preview,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed
            log.warning("commitment confirm request failed err=%s", type(exc).__name__)
            return AskOutcome.FAILED

    async def _request(
        self,
        *,
        tool: str,
        action: str,
        args: Mapping[str, Any],
        step_up: bool,
        rule_id: str,
        tainted: bool,
        preview: str | None,
    ) -> AskOutcome:
        audit_kw: dict[str, Any] = {"tool": tool, "rule_id": rule_id, "step_up": step_up}
        if step_up and self._totp is None:
            await self._audit("request_refused", reason="step_up_not_configured", **audit_kw)
            return AskOutcome.UNAVAILABLE
        try:
            payload = plain_payload(args)
            sha = payload_sha256(tool, action, payload)
        except PayloadError:
            await self._audit("request_refused", reason="invalid_payload", **audit_kw)
            return AskOutcome.INVALID
        now = self._now()
        if await self._store.open_duplicate(sha, now) is not None:
            await self._audit("request_refused", reason="duplicate_open", **audit_kw)
            return AskOutcome.DUPLICATE
        if await self._store.open_count(now) >= self._s.max_open:
            await self._audit("request_refused", reason="too_many_open", **audit_kw)
            return AskOutcome.TOO_MANY
        if (
            tool == COMMITMENT_TOOL
            and await self._store.open_count(now, tool=COMMITMENT_TOOL)
            >= self._s.max_open_commitment
        ):
            await self._audit("request_refused", reason="too_many_commitment", **audit_kw)
            return AskOutcome.TOO_MANY
        expires = (now + timedelta(seconds=self._s.ttl_s)).replace(microsecond=0)
        nonce = secrets.token_bytes(16)
        row_id = await self._store.create(
            tool=tool,
            action=action,
            payload=payload,
            payload_sha256=sha,
            nonce=nonce,
            requires_totp=step_up,
            created_at=now,
            expires_at=expires,
            source_event_id=current_event_id(),
        )
        text = render_card(
            tool=tool,
            action=action,
            payload=payload,
            expires_at=expires,
            now=now,
            offset_minutes=self._s.display_utc_offset_minutes,
            step_up=step_up,
            rule_id=rule_id,
        )
        if preview:
            clipped = reveal(preview[:_MAX_PREVIEW])
            text += f"\nStored text (untrusted, for reference only):\n{clipped}"
        card = ApprovalCard(
            text=text,
            approve_data=tok.encode(
                tok.Verb.APPROVE, row_id, self._mac(tok.Verb.APPROVE, row_id, sha, nonce, expires)
            ),
            reject_data=tok.encode(
                tok.Verb.REJECT, row_id, self._mac(tok.Verb.REJECT, row_id, sha, nonce, expires)
            ),
        )
        try:
            await self._gw.send_approval(card)
        except Exception as exc:  # noqa: BLE001 - card not shown: the row must not stay open
            await self._store.close(row_id, "cancelled")
            await self._audit_quiet(
                "card_failed", action_id=row_id, reason=type(exc).__name__[:64], **audit_kw
            )
            return AskOutcome.FAILED
        await self._audit("requested", action_id=row_id, tainted=tainted, **audit_kw)
        log.info("approval requested action=%s step_up=%s", _short(row_id), step_up)
        return AskOutcome.REQUESTED

    # ------------------------------------------------------------ callbacks

    async def handle_callback(self, update: InboundUpdate) -> None:
        if update.kind is not UpdateKind.CALLBACK:
            return
        if update.from_user_id != self._user:
            # R2: never answer anyone else. The gateway drops these already.
            await self._audit_quiet("callback_rejected", reason="wrong_user")
            log.warning("approval callback from a non-allowlisted user dropped")
            return
        cb = update.callback_id or ""
        data = update.callback_data
        ref = MessageRef(update.message_id) if update.message_id is not None else None
        try:
            if isinstance(data, str) and data.startswith(KEYPAD_PREFIX):
                await self._keypad(cb, data[len(KEYPAD_PREFIX) :])
            else:
                await self._token(cb, data, ref)
        except Exception as exc:  # noqa: BLE001 - fail closed, nothing ran
            log.warning("approval callback failed err=%s", type(exc).__name__)
            self._challenge = None
            await self._answer(cb, FAILED)

    async def _answer(self, cb: str, text: str) -> None:
        if not cb:
            return
        try:
            await self._gw.answer_callback(cb, text)
        except Exception as exc:  # noqa: BLE001 - the toast is cosmetic
            log.warning("answer callback failed err=%s", type(exc).__name__)

    async def _close_card(self, ref: MessageRef | None, outcome: str) -> None:
        if ref is None:
            return
        try:
            await self._gw.close_approval(ref, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.warning("close approval card failed err=%s", type(exc).__name__)

    def _intact(self, row: PendingRow) -> bool:
        """The stored payload still hashes to the stored hash."""
        try:
            actual = payload_sha256(row.tool, row.action, row.payload)
        except PayloadError:
            return False
        return tok.mac_matches(actual.encode("ascii"), row.payload_sha256.encode("ascii"))

    async def _token(self, cb: str, data: object, ref: MessageRef | None) -> None:
        try:
            parsed = tok.parse(data)
        except tok.TokenError:
            await self._audit("callback_rejected", reason="malformed")
            await self._answer(cb, STALE)
            return
        aid = parsed.action_id
        row = await self._store.load(aid)
        if row is None:
            await self._audit("callback_rejected", reason="unknown_action", action_id=aid)
            await self._answer(cb, STALE)
            return
        if not self._intact(row):
            await self._audit("callback_rejected", reason="payload_hash_mismatch", action_id=aid)
            log.error("approval row payload does not match its hash action=%s", _short(aid))
            await self._answer(cb, STALE)
            return
        expected = self._mac(parsed.verb, aid, row.payload_sha256, row.nonce, row.expires_at)
        if not tok.mac_matches(expected, parsed.mac):
            await self._audit("callback_rejected", reason="bad_mac", action_id=aid, tool=row.tool)
            log.warning("approval callback with a bad MAC action=%s", _short(aid))
            await self._answer(cb, STALE)
            return
        if row.status != "pending":
            await self._audit("callback_rejected", reason=f"status_{row.status}", action_id=aid)
            await self._answer(cb, DONE_ALREADY)
            return
        now = self._now()
        if now >= row.expires_at:
            if not await self._store.close(aid, "expired"):
                # Lost a race: the row was closed or consumed after our load.
                await self._audit("callback_rejected", reason="status_changed", action_id=aid)
                await self._answer(cb, DONE_ALREADY)
                return
            await self._audit("expired", action_id=aid, tool=row.tool)
            await self._answer(cb, EXPIRED)
            await self._close_card(ref, "expired")
            await self._notify(row.tool, row.action, row.payload, "expired")
            return
        if parsed.verb is tok.Verb.REJECT:
            if not await self._store.close(aid, "rejected"):
                # Lost a race (e.g. Approve consumed it after our load): the
                # row is not rejected, so nobody may be told it was.
                await self._audit("callback_rejected", reason="status_changed", action_id=aid)
                await self._answer(cb, DONE_ALREADY)
                return
            await self._audit("rejected_by_user", action_id=aid, tool=row.tool)
            await self._answer(cb, REJECTED)
            await self._close_card(ref, "rejected")
            await self._notify(row.tool, row.action, row.payload, "rejected")
            return
        if row.requires_totp:
            await self._start_step_up(cb, row, ref, now)
            return
        consumed = await self._store.consume(
            aid, payload_sha256=row.payload_sha256, now=now, step_up_verified=False
        )
        await self._after_consume(cb, consumed, aid, row.tool, ref)

    # ------------------------------------------------------------- step-up

    async def _locked(self, now: datetime) -> bool:
        since = now - timedelta(seconds=self._s.totp_lockout_window_s)
        return await self._store.totp_failures_since(since) >= self._s.totp_lockout_failures

    async def _start_step_up(
        self, cb: str, row: PendingRow, ref: MessageRef | None, now: datetime
    ) -> None:
        if self._totp is None:
            await self._store.close(row.action_id, "rejected")
            await self._audit("request_refused", reason="step_up_not_configured", tool=row.tool)
            await self._answer(cb, NOT_CONFIGURED)
            await self._close_card(ref, "rejected")
            return
        if await self._locked(now):
            await self._audit("totp_locked", action_id=row.action_id, tool=row.tool)
            await self._answer(cb, LOCKED)
            return
        if await self._store.totp_failures_for(row.action_id) >= self._s.totp_attempts_per_action:
            # Defensive: the row should already be rejected; never re-open it.
            await self._store.close(row.action_id, "rejected")
            await self._audit("rejected_totp", action_id=row.action_id, tool=row.tool)
            await self._answer(cb, REJECTED)
            await self._close_card(ref, "rejected")
            return
        deadline = min(row.expires_at, now + timedelta(seconds=self._s.step_up_window_s))
        tag = secrets.token_hex(4)
        prompt = render_step_up_prompt(
            tool=row.tool,
            entered=0,
            expires_at=deadline,
            offset_minutes=self._s.display_utc_offset_minutes,
        )
        keypad_ref = await self._gw.send_keypad(prompt, keypad_rows(tag))
        self._challenge = _Challenge(tag, row.action_id, row.tool, deadline, keypad_ref, ref)
        await self._audit("step_up_started", action_id=row.action_id, tool=row.tool)
        await self._answer(cb, ENTER_CODE)

    async def _end_challenge(self, ch: _Challenge, outcome: str) -> None:
        self._challenge = None
        await self._close_card(ch.keypad_ref, outcome)

    async def _keypad(self, cb: str, rest: str) -> None:
        ch = self._challenge
        tag, _, key = rest.partition(":")
        if ch is None or not tok.mac_matches(tag.encode(), ch.tag.encode()) or len(key) != 1:
            await self._answer(cb, STALE)
            return
        now = self._now()
        if now >= ch.deadline:
            await self._store.close(ch.action_id, "expired")
            await self._audit("expired", action_id=ch.action_id, tool=ch.tool)
            await self._end_challenge(ch, "expired")
            await self._close_card(ch.card_ref, "expired")
            await self._answer(cb, EXPIRED)
            return
        if key == "c":
            await self._store.close(ch.action_id, "cancelled")
            await self._audit("cancelled", action_id=ch.action_id, tool=ch.tool)
            await self._end_challenge(ch, "cancelled")
            await self._close_card(ch.card_ref, "cancelled")
            await self._answer(cb, CANCELLED)
            return
        if key == "b":
            ch.digits = ch.digits[:-1]
            await self._answer(cb, f"{len(ch.digits)}/6")
            return
        if key not in "0123456789":
            await self._answer(cb, STALE)
            return
        ch.digits += key
        if len(ch.digits) < totp.DIGITS:
            await self._answer(cb, f"{len(ch.digits)}/6")
            return
        code, ch.digits = ch.digits, ""
        await self._verify_code(cb, ch, code, now)

    async def _verify_code(self, cb: str, ch: _Challenge, code: str, now: datetime) -> None:
        aid = ch.action_id
        if self._totp is None or await self._locked(now):
            await self._audit("totp_locked", action_id=aid, tool=ch.tool)
            await self._end_challenge(ch, "locked")
            await self._answer(cb, LOCKED)
            return
        row = await self._store.load(aid)
        if row is None or row.status != "pending" or not self._intact(row) or now >= row.expires_at:
            await self._audit("callback_rejected", reason="not_pending", action_id=aid)
            await self._end_challenge(ch, "closed")
            await self._answer(cb, STALE)
            return
        step = totp.verify(
            self._totp, code, epoch_s=now.timestamp(), after_step=await self._store.last_totp_step()
        )
        if step is None:
            # Durable BEFORE the answer: the lockout counts this failure even
            # if the process dies right after.
            await self._audit(TOTP_FAILED, action_id=aid, tool=ch.tool)
            # Per ACTION, counted durably (policy_audit): a fresh Approve tap
            # (new challenge) or a restart does not reset the budget.
            if await self._store.totp_failures_for(aid) >= self._s.totp_attempts_per_action:
                await self._store.close(aid, "rejected")
                await self._audit("rejected_totp", action_id=aid, tool=ch.tool)
                await self._end_challenge(ch, "rejected")
                await self._close_card(ch.card_ref, "rejected")
                await self._answer(cb, f"{WRONG_CODE} {REJECTED}")
                return
            await self._answer(cb, f"{WRONG_CODE} Try again.")
            return
        await self._audit(TOTP_OK, action_id=aid, tool=ch.tool, totp_step=step)
        self._challenge = None
        await self._close_card(ch.keypad_ref, "verified")
        consumed = await self._store.consume(
            aid, payload_sha256=row.payload_sha256, now=now, step_up_verified=True
        )
        await self._after_consume(cb, consumed, aid, ch.tool, ch.card_ref)

    # ------------------------------------------------------------ execution

    async def _after_consume(
        self,
        cb: str,
        consumed: ConsumedApproval | None,
        aid: uuid.UUID,
        tool: str,
        ref: MessageRef | None,
    ) -> None:
        if consumed is None:
            await self._audit("callback_rejected", reason="consume_refused", action_id=aid)
            await self._answer(cb, STALE)
            return
        await self._audit("consumed", action_id=aid, tool=tool, step_up=consumed.step_up_verified)
        await self._answer(cb, "Approved. Running it now.")
        status, rule = await self._execute(consumed)
        executed = status == "ok"
        await self._audit_quiet(
            "executed" if executed else "exec_failed",
            action_id=aid,
            tool=tool,
            outcome=status,
            rule_id=rule,
            executed=executed,
        )
        log.info("approval executed action=%s status=%s", _short(aid), status)
        await self._close_card(ref, f"done: {status}" if executed else f"not run ({status})")
        await self._notify(consumed.tool, consumed.action, consumed.payload, "done")

    async def _execute(self, c: ConsumedApproval) -> tuple[str, str]:
        try:
            if c.action == TOOL_CALL_ACTION and self._executor is not None:
                with turn_event(c.source_event_id):
                    res = await self._executor.execute_approved(c)
                return str(res.status), str(res.rule_id)
            if c.tool == MEMORY_TOOL and c.action == PROMOTE_ACTION and self._promoter is not None:
                item = uuid.UUID(str(c.payload.get("item_id")))
                await self._promoter(item, c.action_id)
                return "ok", "approval.memory_promote"
            if (
                c.tool == COMMITMENT_TOOL
                and c.action == CONFIRM_ACTION
                and self._confirmer is not None
            ):
                cand = uuid.UUID(str(c.payload.get("candidate_id")))
                await self._confirmer(cand, c.action_id)
                return "ok", "approval.commitment_confirm"
        except Exception as exc:  # noqa: BLE001 - consumed but failed: nothing retried
            log.warning("approved action failed err=%s", type(exc).__name__)
            return "error", "exec.error"
        return "error", "exec.no_executor"
