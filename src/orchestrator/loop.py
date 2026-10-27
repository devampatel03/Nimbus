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
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from src.lanes.data import BlockOrigin, default_nonce, render_data_block
from src.lanes.quarantine import Quarantine, local_data_block
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
    Dispatcher,
    DispatchResult,
    LoopTool,
    Policy,
    safe_name,
)
from src.orchestrator.exposure import ExposableTool, names, select_tools
from src.personality.agent_character import AgentStance
from src.personality.user_profile import UserTrait

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
    ) -> None:
        self._llm = llm
        self._quarantine = quarantine
        self._by_name: Mapping[str, LoopTool] = {t.spec.name: t for t in tools}
        self._dispatcher = Dispatcher(self._by_name, policy)
        self._card = character_card
        self._recall = recall
        self._cfg = config or LoopConfig()
        self._always = always_expose
        self._nonce = nonce_factory

    async def run_turn(
        self,
        user_text: str,
        *,
        history: Sequence[ChatMessage] = (),
        traits: Sequence[UserTrait] = (),
        stances: Sequence[AgentStance] = (),
        turn_id: uuid.UUID | None = None,
    ) -> TurnResult:
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
        )
        taint = ctx.taint
        exposed = select_tools(
            [ExposableTool(t.spec, t.spec.name in self._always) for t in self._by_name.values()],
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
                            tainted=taint.tainted,
                        )
                    )
                    messages.append(
                        ChatMessage(role="tool", content=CALL_CAP_MESSAGE, tool_call_id=call.id)
                    )
                    continue
                msg = await self._run_call(call, exposed_names, events, taint)
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
    ) -> ChatMessage:
        result = await self._dispatcher.dispatch(
            call.name, call.arguments, exposed=exposed, tainted=taint.tainted
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
        return ChatMessage(role="tool", content=content, tool_call_id=call.id)
