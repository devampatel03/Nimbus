# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`GuardedLLM`: the one `LLMClient` implementation (INV-1, R4, R5, INV-7).

Every call runs, in this order, before the provider is touched:

1. kill switch (`system_flag`)           -> LLMRefused("kill_switch")
2. lane check (QUARANTINE => no tools)   -> LLMRefused("lane_violation")
3. scrub outbound text                   -> LLMRefused("scrub_blocked") on a
   secret/canary; card/Aadhaar/OTP are redacted in place
4. spend cap (daily, INR, IST day)       -> SpendCapExceeded; flips the spend
   mode to READ_ONLY for the rest of the IST day and fires the alert hook once
5. provider call (the only transport use)
6. audit row (`llm_audit` shape) + cost accounting

A refusal in steps 1-4 writes a `blocked` audit row and makes ZERO provider
calls. Any unexpected guard failure also refuses (fail closed). If the audit
row cannot be written for a successful call the result is withheld.

Known limitation: `llm_audit` is append-only with a status CHECK of
ok|blocked|error, so there is no "started" row; the single row is written once
the call finishes. A crash mid-call therefore leaves that call's cost
unrecorded (bounded by one call's `max_output_tokens`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from src.llm.pricing import PriceTable, estimate_tokens
from src.llm.protocols import (
    AlertHook,
    AuditRecord,
    AuditWriter,
    Clock,
    FlagStore,
    Provider,
    SpendLedger,
)
from src.llm.scrub import Scrubber, ScrubBlocked
from src.llm.settings import DEFAULT_API_KEY, LLMSettings
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    Lane,
    LLMAuditError,
    LLMMode,
    LLMProviderError,
    LLMRefused,
    LLMResponseError,
    ProviderResponse,
    SpendCapExceeded,
    Usage,
)

logger = logging.getLogger("nimbus.llm")

IST = ZoneInfo("Asia/Kolkata")
KILL_SWITCH_KEY = "llm_kill_switch"  # value {"engaged": bool}
SPEND_MODE_KEY = "llm_spend_mode"  # value {"mode": "read_only", "day": "YYYY-MM-DD" (IST)}
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_NOT_SENT_CODES = frozenset({"egress_denied", "egress_failed", "egress_config"})
_SENT_STATUSES = frozenset({408, 504})
_REPAIR_PROMPT = (
    "Your previous reply was not valid JSON for the required schema. "
    "Reply again with only the corrected JSON object."
)


_R = TypeVar("_R")
_T = TypeVar("_T")


@dataclass(frozen=True)
class CallMeta:
    """Identity of one billed call, for audit rows and IST-day bookkeeping."""

    purpose: str
    provider: str
    model: str
    started: datetime
    turn_id: uuid.UUID | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def ist_day_bounds(now: datetime) -> tuple[str, datetime, datetime]:
    """(IST date string, start, end) of the IST day containing `now`, as UTC."""
    local = now.astimezone(IST)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.date().isoformat(),
        start_local.astimezone(UTC),
        end_local.astimezone(UTC),
    )


def _message_text(m: ChatMessage) -> str:
    parts = [m.content or ""]
    parts.extend(json.dumps(tc.arguments, sort_keys=True) for tc in m.tool_calls)
    return "\n".join(p for p in parts if p)


class CallAccountant:
    """The ONE implementation of R5 accounting, shared by `GuardedLLM.chat` and
    `ApiEmbedder` (src/llm/embeddings_api.py).

    reserve (pre-check vs cap, in-flight) -> send -> bill/hold -> shielded audit
    -> release. A reservation is released only after a COMMITTED audit row; a
    cancelled call, a failed audit write, or a possibly-sent failure keeps it
    held for the rest of that IST day (`_held`), and held spend counts in
    `reserve`. Fixes belong here, never in a copy.
    """

    def __init__(
        self,
        *,
        flags: FlagStore,
        audit: AuditWriter,
        ledger: SpendLedger,
        settings: LLMSettings,
        pricing: PriceTable,
        alert: AlertHook | None,
        clock: Clock,
    ) -> None:
        self._flags = flags
        self._audit = audit
        self._ledger = ledger
        self._settings = settings
        self._pricing = pricing
        self._alert = alert
        self.clock = clock
        self._inflight = Decimal(0)
        # Reservations whose audit row could not be written: the spend is real
        # but unrecorded, so they stay counted for the rest of that IST day.
        self._held = Decimal(0)
        self._held_day = ""
        self._lock = asyncio.Lock()

    @property
    def pricing(self) -> PriceTable:
        return self._pricing

    # -------------------------------------------------------------- state

    async def mode(self) -> LLMMode:
        """NORMAL | READ_ONLY | KILLED. Unreadable state fails closed (KILLED)."""
        try:
            if await self._kill_engaged():
                return LLMMode.KILLED
            day, _, _ = ist_day_bounds(self.clock())
            if await self._read_only_today(day):
                return LLMMode.READ_ONLY
        except Exception:  # noqa: BLE001 - fail closed
            return LLMMode.KILLED
        return LLMMode.NORMAL

    async def check_kill_switch(self) -> None:
        try:
            engaged = await self._kill_engaged()
        except Exception:  # noqa: BLE001 - unreadable switch => treat as engaged
            raise LLMRefused("guard_error") from None
        if engaged:
            raise LLMRefused("kill_switch")

    async def _kill_engaged(self) -> bool:
        flag = await self._flags.get(KILL_SWITCH_KEY)
        if flag is None:
            return False
        # Fail closed: only an explicit boolean False disengages the switch;
        # malformed values ("true", 1, missing key) count as engaged.
        return flag.get("engaged") is not False

    async def _read_only_today(self, day: str) -> bool:
        flag = await self._flags.get(SPEND_MODE_KEY)
        return bool(flag and flag.get("mode") == "read_only" and flag.get("day") == day)

    # ------------------------------------------------------------ reserve

    async def reserve(self, estimates: Sequence[Decimal], now: datetime) -> list[Decimal]:
        """Check the cap for the SUM of `estimates` and reserve them all at
        once (one reservation per planned send). Raises `SpendCapExceeded`."""
        day, start, end = ist_day_bounds(now)
        cap = self._settings.daily_spend_cap_inr
        total = sum(estimates, Decimal(0))
        async with self._lock:
            if await self._read_only_today(day):
                raise SpendCapExceeded
            spent = await self._ledger.spent_between(start, end)
            held = self._held if self._held_day == day else Decimal(0)
            if spent + self._inflight + held + total > cap:
                await self._trip_cap(day, spent, cap)
                raise SpendCapExceeded
            self._inflight += total
        return list(estimates)

    async def release_unused(self, amount: Decimal) -> None:
        """Release reservations that were never sent (no billing)."""
        if amount:
            async with self._lock:
                self._inflight -= amount

    async def _trip_cap(self, day: str, spent: Decimal, cap: Decimal) -> None:
        await self._flags.set(
            SPEND_MODE_KEY,
            {"mode": "read_only", "day": day, "spent_inr": str(spent), "cap_inr": str(cap)},
            updated_by="llm_guard",
        )
        logger.warning("llm daily spend cap reached; mode=read_only day=%s", day)
        if self._alert is not None:
            try:
                await self._alert(
                    f"LLM daily spend cap reached (INR {spent} of {cap}). "
                    "LLM calls are paused until midnight IST; reminders keep firing."
                )
            except Exception:  # noqa: BLE001 - alert failure must not mask the refusal
                logger.error("llm spend-cap alert hook failed")

    # ---------------------------------------------------------- preflight

    async def preflight(self, meta: CallMeta, checks: Callable[[], Awaitable[_T]]) -> _T:
        """Run pre-send checks. Any `LLMRefused` writes a `blocked` row and
        propagates; any other failure refuses (fail closed). Zero sends."""
        try:
            return await checks()
        except LLMRefused as exc:
            await self.audit_safely(meta, "blocked", exc.reason)
            raise
        except Exception:  # noqa: BLE001 - any guard bug must fail closed
            logger.error("llm guard error; refusing call")
            await self.audit_safely(meta, "blocked", "guard_error")
            raise LLMRefused("guard_error") from None

    # -------------------------------------------------------------- billed

    async def billed(
        self,
        meta: CallMeta,
        reserved: Decimal,
        send: Callable[[], Awaitable[_R]],
        finish: Callable[[_R], Awaitable[_T]],
    ) -> _T:
        """send -> bill/hold -> shielded audit -> release, for ONE send whose
        `reserved` amount was already taken by `reserve`. `finish` writes the
        ok audit row and raises `LLMAuditError` if it cannot."""
        keep = False
        try:
            # From here the request may be billed. Hold the reservation until
            # a committed audit row is CONFIRMED (cancel/crash/failed write all
            # leave it held for the IST day).
            keep = True
            try:
                resp = await send()
            except asyncio.CancelledError:
                await asyncio.shield(self.audit_error(meta, None, reserved))
                raise
            except LLMResponseError as exc:
                # The transport answered (and billed) but the body was unusable:
                # bill the parsed usage, else the reserved estimate (pessimistic).
                keep = not await asyncio.shield(self.audit_error(meta, exc.usage, reserved))
                raise
            except LLMProviderError as exc:
                # Sent-but-no-body failures (timeout/connection/unexpected, 408/504)
                # may be billed; other statuses and pre-send egress failures not.
                sent = exc.code not in _NOT_SENT_CODES and (
                    exc.status is None or exc.status in _SENT_STATUSES
                )
                wrote = await asyncio.shield(
                    self.audit_error(meta, None, reserved if sent else Decimal(0))
                )
                keep = sent and not wrote
                raise
            except Exception as exc:
                # Answered but rejected by the caller's own validation: bill the
                # usage it attached, else the reserved estimate.
                usage = getattr(exc, "usage", None)
                keep = not await asyncio.shield(
                    self.audit_error(meta, usage if isinstance(usage, Usage) else None, reserved)
                )
                raise
            # The reservation is released only AFTER the audit row is committed
            # (in `finally`), so the ledger never under-counts in between (R5).
            result = await asyncio.shield(finish(resp))
            keep = False  # reached only if the audit row committed
            return result
        finally:
            if reserved:
                async with self._lock:
                    self._inflight -= reserved
                    if keep:
                        day, _, _ = ist_day_bounds(meta.started)
                        if self._held_day != day:
                            self._held, self._held_day = Decimal(0), day
                        self._held += reserved

    # --------------------------------------------------------------- audit

    def cost_inr(self, model: str, usage: Usage) -> Decimal:
        return self._pricing.cost_inr(model, usage.tokens_in, usage.tokens_out)

    async def write_ok(
        self, meta: CallMeta, usage: Usage, payload: str | None = None
    ) -> tuple[uuid.UUID, Decimal]:
        """Write the single ok row; `LLMAuditError` if it cannot be written."""
        cost = self.cost_inr(meta.model, usage)
        now = self.clock()
        record = AuditRecord(
            purpose=meta.purpose,
            provider=meta.provider,
            model=meta.model,
            status="ok",
            created_at=now,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cost_inr=cost,
            latency_ms=_ms(now - meta.started),
            turn_id=meta.turn_id,
            payload_redacted=payload,
        )
        try:
            audit_id = await self._audit.write(record)
        except Exception:  # noqa: BLE001
            logger.error("llm audit write failed; withholding result")
            raise LLMAuditError from None
        logger.info(
            "llm call purpose=%s model=%s status=ok tokens_in=%d tokens_out=%d cost_inr=%s",
            record.purpose,
            meta.model,
            usage.tokens_in,
            usage.tokens_out,
            cost,
        )
        return audit_id, cost

    async def audit_error(self, meta: CallMeta, usage: Usage | None, reserved: Decimal) -> bool:
        """Write the error row; True iff it was committed."""
        if usage is not None and (usage.tokens_in or usage.tokens_out):
            cost = self.cost_inr(meta.model, usage)
            return await self.audit_safely(meta, "error", None, usage, cost)
        return await self.audit_safely(meta, "error", None, None, reserved)

    async def audit_safely(
        self,
        meta: CallMeta,
        status: str,
        reason: str | None,
        usage: Usage | None = None,
        cost: Decimal = Decimal(0),
    ) -> bool:
        now = self.clock()
        record = AuditRecord(
            purpose=meta.purpose,
            provider=meta.provider,
            model=meta.model,
            status=status,
            created_at=now,
            blocked_reason=reason,
            tokens_in=usage.tokens_in if usage else 0,
            tokens_out=usage.tokens_out if usage else 0,
            cost_inr=cost,
            latency_ms=_ms(now - meta.started),
            turn_id=meta.turn_id,
        )
        written = True
        try:
            await self._audit.write(record)
        except Exception:  # noqa: BLE001 - never mask the original failure
            written = False
            logger.error("llm audit write failed for status=%s", status)
        logger.info(
            "llm call purpose=%s model=%s status=%s reason=%s",
            record.purpose,
            meta.model,
            status,
            reason,
        )
        return written


class GuardedLLM:
    def __init__(
        self,
        provider: Provider,
        *,
        flags: FlagStore,
        audit: AuditWriter,
        ledger: SpendLedger,
        settings: LLMSettings | None = None,
        scrubber: Scrubber | None = None,
        pricing: PriceTable | None = None,
        alert: AlertHook | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings if settings is not None else LLMSettings()
        self._scrubber = (
            scrubber
            if scrubber is not None
            else Scrubber(
                canaries=self._settings.canaries,
                secrets=_real_secrets(self._settings),
            )
        )
        self._pricing = pricing if pricing is not None else PriceTable(self._settings.price_table)
        self._clock: Clock = clock if clock is not None else _utc_now
        self._acct = CallAccountant(
            flags=flags,
            audit=audit,
            ledger=ledger,
            settings=self._settings,
            pricing=self._pricing,
            alert=alert,
            clock=self._clock,
        )

    @property
    def accountant(self) -> CallAccountant:
        """The ONE accountant. Anything else that spends on the same provider
        (the API embedder) must be built with this so in-flight and held spend
        is shared and the daily cap cannot be overshot once per accountant."""
        return self._acct

    # ------------------------------------------------------------------ API

    async def chat(self, req: ChatRequest) -> ChatResult:
        schema = req.response_schema
        current = req
        for attempt in (0, 1):
            result = await self._guarded_call(current)
            if schema is None or result.tool_calls:
                return result
            parsed = _parse_schema(result.text, schema)
            if parsed is not None:
                return result.model_copy(update={"parsed": parsed})
            if attempt == 1:
                raise LLMResponseError("schema_mismatch")
            # One repair retry; a full guarded call of its own (audited, capped).
            current = current.model_copy(
                update={
                    "messages": (
                        *current.messages,
                        ChatMessage(role="assistant", content=result.text or ""),
                        ChatMessage(role="user", content=_REPAIR_PROMPT),
                    )
                }
            )
        raise LLMResponseError("schema_mismatch")  # pragma: no cover

    async def mode(self) -> LLMMode:
        return await self._acct.mode()

    # ------------------------------------------------------------ the guard

    async def _guarded_call(self, req: ChatRequest) -> ChatResult:
        started = self._clock()
        model = self._provider.model
        meta = CallMeta(req.purpose.value, self._provider.name, model, started, req.turn_id)

        async def checks() -> tuple[ChatRequest, str | None, Decimal]:
            await self._acct.check_kill_switch()
            self._check_lane(req)
            scrubbed, payload = self._scrub(req)
            chars = sum(len(_message_text(m)) for m in req.messages) + sum(
                len(t.description) + len(json.dumps(t.parameters)) for t in req.tools
            )
            estimate = self._pricing.estimate_inr(model, chars, req.max_output_tokens)
            (reserved,) = await self._acct.reserve([estimate], started)
            return scrubbed, payload, reserved

        scrubbed, payload, reserved = await self._acct.preflight(meta, checks)

        async def finish(resp: ProviderResponse) -> ChatResult:
            if req.lane is Lane.QUARANTINE and resp.tool_calls:
                logger.warning("quarantine lane: dropping provider-invented tool calls")
                resp = resp.model_copy(update={"tool_calls": ()})
            return await self._finish(req, meta, resp, payload)

        return await self._acct.billed(
            meta, reserved, lambda: self._provider.complete(scrubbed), finish
        )

    @staticmethod
    def _check_lane(req: ChatRequest) -> None:
        if req.lane is Lane.QUARANTINE and req.tools:
            raise LLMRefused("lane_violation")

    def _scrub(self, req: ChatRequest) -> tuple[ChatRequest, str | None]:
        out: list[ChatMessage] = []
        try:
            for m in req.messages:
                content = m.content
                if content is not None:
                    content = self._scrubber.scrub(content).text
                calls = []
                for ident in (
                    m.tool_call_id,
                    *(c.id for c in m.tool_calls),
                    *(c.name for c in m.tool_calls),
                ):
                    if ident:
                        self._scrubber.check(ident)
                for tc in m.tool_calls:
                    args_json = json.dumps(tc.arguments, sort_keys=True)
                    clean = self._scrubber.scrub(args_json).text
                    calls.append(
                        tc
                        if clean == args_json
                        else tc.model_copy(update={"arguments": json.loads(clean)})
                    )
                out.append(m.model_copy(update={"content": content, "tool_calls": tuple(calls)}))
            for t in req.tools:
                self._scrubber.check(t.name)
                self._scrubber.check(t.description)
                self._scrubber.check(json.dumps(t.parameters))
        except ScrubBlocked:
            raise LLMRefused("scrub_blocked") from None
        except json.JSONDecodeError:
            # Redaction broke the argument JSON: refuse rather than send it.
            raise LLMRefused("scrub_blocked") from None
        scrubbed = req.model_copy(update={"messages": tuple(out)})
        payload = (
            "\n".join(f"{m.role}: {_message_text(m)}" for m in out)
            if self._settings.store_payload
            else None
        )
        return scrubbed, payload

    async def _finish(
        self, req: ChatRequest, meta: CallMeta, resp: ProviderResponse, payload: str | None
    ) -> ChatResult:
        usage = resp.usage
        if usage.tokens_in == 0 and usage.tokens_out == 0:
            # Provider reported nothing: bill the estimate rather than zero.
            usage = Usage(
                tokens_in=estimate_tokens(sum(len(_message_text(m)) for m in req.messages)),
                tokens_out=estimate_tokens(len(resp.text or "")),
            )
        audit_id, cost = await self._acct.write_ok(meta, usage, payload)
        return ChatResult(
            text=resp.text,
            tool_calls=resp.tool_calls,
            usage=usage,
            cost_inr=cost,
            audit_id=audit_id,
        )


def _real_secrets(settings: LLMSettings) -> tuple[str, ...]:
    key = settings.api_key.get_secret_value()
    return () if key == DEFAULT_API_KEY else (key,)


def _ms(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() * 1000))


def _parse_schema(text: str | None, schema: type[BaseModel]) -> BaseModel | None:
    if not text:
        return None
    body = text.strip()
    m = _FENCE_RE.match(body)
    if m:
        body = m.group(1)
    try:
        return schema.model_validate_json(body)
    except (ValidationError, ValueError):
        return None


__all__ = [
    "KILL_SWITCH_KEY",
    "SPEND_MODE_KEY",
    "CallAccountant",
    "CallMeta",
    "GuardedLLM",
    "ist_day_bounds",
]
