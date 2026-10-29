# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Plain async tool loop (ADR-0006). No LangGraph.

The only model access is an injected `LLMClient` (the guarded `src.llm`
module), trusted lane, purpose chat. Per turn: build context, select at most
12 tools, then loop up to `max_iterations` model calls within `token_budget`.
Each requested tool call goes through the `Dispatcher` (deterministic
policy, INV-5). When a limit is hit, or the guard refuses/fails, the turn
ends with a short fixed message rather than an exception (fail closed).

P3.0 (ADR-0010): untrusted tool output enters only as a lanes `DataBlock`, and
the turn's `Taint` is marked whenever untrusted-derived data enters (recalled
untrusted memory, untrusted tool output, tool errors). Every later dispatch in
the turn receives `tainted=True`; the result reports it for the next turn.
P3.0 MED-D: one `QuarantineBudget` per turn (`max_quarantine_calls_per_turn`)
bounds every quarantine call in the turn, memory and tool output together;
items past it are withheld (`over_budget`) without a model call.

P3.2: `approvals` (an `ApprovalRequester`) turns a `require_approval` verdict
into a card for the user; the call still does not run in this turn. `audit`
persists every decision (`policy_audit`).

P3.1: the caller passes `carried_taint` (ADR-0010 cross-turn window, owned by
`src.policy.TaintCarry`); a carried turn dispatches every call as tainted.
`TurnResult.tainted` stays the turn's OWN taint so the carry can expire. The
dispatcher reads the guard's mode (`llm.mode`) for the policy context.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from src.lanes.data import BlockOrigin, DataBlock, default_nonce, render_data_block
from src.lanes.quarantine import (
    MAX_QUARANTINE_CALLS_PER_TURN,
    Quarantine,
    QuarantineBudget,
    QuarantineError,
    local_data_block,
    withheld_block,
)
from src.lanes.schemas import SCHEMA_FOR_SOURCE
from src.lanes.taint import Taint, TaintSource
from src.llm.protocols import LLMClient
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    Lane,
    LLMError,
    Purpose,
    ToolCallRequest,
)
from src.orchestrator.context import MemoryRecall, build_context
from src.orchestrator.dispatch import (
    CALL_CAP_MESSAGE,
    ApprovalRequester,
    AuditSink,
    Dispatcher,
    DispatchResult,
    LoopTool,
    Policy,
    safe_name,
)
from src.orchestrator.exposure import ExposableTool, names, select_tools
from src.personality.agent_character import AgentStance
from src.personality.user_profile import UserTrait
from src.policy import LOCAL_SCOPES, TOOL_RULES

log = logging.getLogger(__name__)

BUDGET_MESSAGE = (
    "I hit my limit for this request before finishing. Ask again or break it into a smaller step."
)
UNAVAILABLE_MESSAGE = "I can't reach my language model right now. Please try again shortly."
OUTPUT_BUDGET_MESSAGE = "[tool output omitted: per-turn output budget reached]"
EMPTY_MESSAGE = "I have nothing to add."


@dataclass(frozen=True)
class LoopConfig:
    max_iterations: int = 5
    token_budget: int = 6000
    max_tools: int = 12
    max_output_tokens: int = 800
    max_tool_calls_per_turn: int = 8
    max_tool_output_chars_per_turn: int = 16000
    # P3.0 MED-D: hard cap on quarantine calls per turn (memory + tool output).
    max_quarantine_calls_per_turn: int = MAX_QUARANTINE_CALLS_PER_TURN

    def __post_init__(self) -> None:
        QuarantineBudget(self.max_quarantine_calls_per_turn)  # validates type and range


@dataclass(frozen=True)
class TurnResult:
    text: str
    status: str  # ok | budget_exceeded | unavailable
    tool_events: tuple[DispatchResult, ...] = ()
    tokens_used: int = 0
    messages: tuple[ChatMessage, ...] = field(default=())
    tainted: bool = False  # untrusted-derived data entered this turn (ADR-0010)


class Orchestrator:
    def __init__(
        self,
        llm: LLMClient,
        tools: Sequence[LoopTool],
        policy: Policy,
        *,
        character_card: str,
        recall: MemoryRecall | None = None,
        config: LoopConfig | None = None,
        always_expose: frozenset[str] = frozenset(),
        nonce_factory: Callable[[], str] | None = None,
        quarantine: Quarantine | None = None,
        approvals: ApprovalRequester | None = None,
        audit: AuditSink | None = None,
        scopes_enabled: frozenset[str] = LOCAL_SCOPES,
    ) -> None:
        self._llm = llm
        self._scopes = frozenset(scopes_enabled)
        self._quarantine = quarantine
        self._by_name: Mapping[str, LoopTool] = {t.spec.name: t for t in tools}
        self._dispatcher = Dispatcher(
            self._by_name,
            policy,
            mode_source=llm.mode,
            scopes_enabled=self._scopes,
            approvals=approvals,
            audit=audit,
        )
        self._card = character_card
        self._recall = recall
        self._cfg = config or LoopConfig()
        self._always = always_expose
        self._nonce = nonce_factory

    def _offerable(self, tool: LoopTool) -> bool:
        """Offer only tools that could run: bound, and (when the policy table
        knows the tool) in an enabled scope. Hidden tools are not exposed, so a
        call to one is still denied (`deny.unexposed`). Fail closed: an
        `available()` that raises, or a non-callable `available`, hides the tool."""
        rule = TOOL_RULES.get(tool.spec.name)
        if rule is not None and rule.scope not in self._scopes:
            return False
        if not hasattr(tool, "available"):
            return True
        check = getattr(tool, "available")  # noqa: B009
        if not callable(check):
            return False  # a non-callable `available` is a bug: hide the tool
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 - fail closed
            return False

    @property
    def dispatcher(self) -> Dispatcher:
        """For `ApprovalService.bind_executor` only (runs consumed approvals)."""
        return self._dispatcher

    async def run_turn(
        self,
        user_text: str,
        *,
        history: Sequence[ChatMessage] = (),
        traits: Sequence[UserTrait] = (),
        stances: Sequence[AgentStance] = (),
        turn_id: uuid.UUID | None = None,
        carried_taint: bool = False,
    ) -> TurnResult:
        carried = carried_taint is not False  # fail closed
        budget = QuarantineBudget(self._cfg.max_quarantine_calls_per_turn)  # one per turn
        ctx = await build_context(
            character_card=self._card,
            user_text=user_text,
            traits=traits,
            stances=stances,
            recall=self._recall,
            history=history,
            nonce_factory=self._nonce,
            quarantine=self._quarantine,
            turn_id=turn_id,
            budget=budget,
        )
        taint = ctx.taint
        exposed = select_tools(
            [
                ExposableTool(t.spec, t.spec.name in self._always)
                for t in self._by_name.values()
                if self._offerable(t)
            ],
            user_text,
            limit=self._cfg.max_tools,
        )
        exposed_names = names(exposed)
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=ctx.system_prompt),
            *ctx.history,
            ChatMessage(role="user", content=user_text),
        ]
        events: list[DispatchResult] = []
        used = 0
        attempted = 0
        out_chars = 0

        def done(text: str, status: str) -> TurnResult:
            return TurnResult(text, status, tuple(events), used, tuple(messages), taint.tainted)

        for _ in range(self._cfg.max_iterations):
            if used >= self._cfg.token_budget:
                return done(BUDGET_MESSAGE, "budget_exceeded")
            remaining = self._cfg.token_budget - used
            req = ChatRequest(
                purpose=Purpose.CHAT,
                lane=Lane.TRUSTED,
                messages=tuple(messages),
                tools=exposed,
                # Remaining budget bounds the output; a call can still overshoot by
                # its input tokens, which are only known after the fact.
                max_output_tokens=max(1, min(self._cfg.max_output_tokens, remaining)),
                turn_id=turn_id,
            )
            try:
                res = await self._llm.chat(req)
            except LLMError as e:
                log.warning("llm unavailable for turn: %s", type(e).__name__)
                return done(UNAVAILABLE_MESSAGE, "unavailable")
            except Exception as e:  # noqa: BLE001 - fail closed, type name only
                log.warning("unexpected error in turn: %s", type(e).__name__)
                return done(UNAVAILABLE_MESSAGE, "unavailable")
            used += res.usage.tokens_in + res.usage.tokens_out
            if not res.tool_calls:
                return done(res.text or EMPTY_MESSAGE, "ok")
            messages.append(
                ChatMessage(role="assistant", content=res.text, tool_calls=res.tool_calls)
            )
            for call in res.tool_calls:
                attempted += 1
                if attempted > self._cfg.max_tool_calls_per_turn:
                    log.info(
                        "dispatch decision tool=%s verdict=deny rule_id=deny.call_cap",
                        safe_name(call.name),
                    )
                    events.append(
                        DispatchResult(
                            call.name,
                            "denied",
                            CALL_CAP_MESSAGE,
                            rule_id="deny.call_cap",
                            tainted=taint.tainted or carried,
                        )
                    )
                    messages.append(
                        ChatMessage(role="tool", content=CALL_CAP_MESSAGE, tool_call_id=call.id)
                    )
                    continue
                msg = await self._run_call(
                    call, exposed_names, events, taint, carried, budget, turn_id
                )
                remaining_chars = self._cfg.max_tool_output_chars_per_turn - out_chars
                if len(msg.content or "") > remaining_chars:
                    msg = ChatMessage(
                        role="tool", content=OUTPUT_BUDGET_MESSAGE, tool_call_id=call.id
                    )
                out_chars += len(msg.content or "")
                messages.append(msg)
        return done(BUDGET_MESSAGE, "budget_exceeded")

    async def _run_call(
        self,
        call: ToolCallRequest,
        exposed: frozenset[str],
        events: list[DispatchResult],
        taint: Taint,
        carried: bool,
        budget: QuarantineBudget,
        turn_id: uuid.UUID | None,
    ) -> ChatMessage:
        result = await self._dispatcher.dispatch(
            call.name, call.arguments, exposed=exposed, tainted=taint.tainted or carried
        )
        events.append(result)
        content = result.content
        if result.untrusted:
            is_error = result.status == "error"
            taint.mark(TaintSource.TOOL_ERROR if is_error else TaintSource.UNTRUSTED_TOOL_OUTPUT)
            block = local_data_block(
                f"tool.{call.name}",
                content,
                origin=BlockOrigin.LOCAL_TOOL_ERROR if is_error else BlockOrigin.LOCAL_TOOL_OUTPUT,
                untrusted=True,
            )
            taint.absorb(block)
            content = render_data_block(block, self._nonce or default_nonce)
        extra = await self._external_blocks(call.name, result, taint, budget, turn_id)
        if extra:
            content = "\n\n".join([content, *extra])
        return ChatMessage(role="tool", content=content, tool_call_id=call.id)

    async def _external_blocks(
        self,
        name: str,
        result: DispatchResult,
        taint: Taint,
        budget: QuarantineBudget,
        turn_id: uuid.UUID | None,
    ) -> list[str]:
        """Quarantine each external item (H18 interim default: every item, no
        bypass); withhold on failure, without a quarantine, over the item cap,
        or once the turn's quarantine budget is spent (`over_budget`, one fixed
        notice per item so positions are kept; no model call)."""
        if not result.external and not result.external_dropped:
            return []
        taint.mark(TaintSource.UNTRUSTED_TOOL_OUTPUT)
        label = f"tool.{name}.external"
        blocks: list[DataBlock] = []
        for item in result.external:
            if self._quarantine is None:
                blocks.append(withheld_block(label, reason="quarantine_unavailable"))
                continue
            try:
                out = await self._quarantine.extract(
                    item, SCHEMA_FOR_SOURCE[item.source], turn_id=turn_id, budget=budget
                )
            except QuarantineError as e:
                blocks.append(withheld_block(label, reason=e.code))
                continue
            taint.mark(TaintSource.QUARANTINE_OUTPUT)
            blocks.append(out.data_block())
        if result.external_dropped:
            blocks.append(withheld_block(label, reason="over_item_cap"))
        rendered: list[str] = []
        for block in blocks:
            taint.absorb(block)
            rendered.append(render_data_block(block, self._nonce or default_nonce))
        return rendered
