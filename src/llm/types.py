# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Value types and errors of the `llm` module (TARGET_DESIGN section 2)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TOOLS = 12  # R16


class Lane(StrEnum):
    TRUSTED = "trusted"
    QUARANTINE = "quarantine"
    SYSTEM = "system"  # scheduler/extraction, no user turn


class Purpose(StrEnum):
    CHAT = "chat"
    QUARANTINE_EXTRACT = "quarantine_extract"
    TRANSCRIBE = "transcribe"
    MEMORY_EXTRACT = "memory_extract"
    FRICTION = "friction"
    EMBED = "embed"
    EVAL = "eval"


class LLMMode(StrEnum):
    NORMAL = "normal"
    READ_ONLY = "read_only"  # daily spend cap hit (R5)
    KILLED = "killed"  # /panic


class LLMError(Exception):
    """Base class. Messages are fixed codes only (INV-7): never prompt text,
    provider error bodies or credentials."""


class LLMRefused(LLMError):
    """Raised BEFORE any network I/O. `reason` is one of kill_switch,
    spend_cap, scrub_blocked, lane_violation, rate_limited, guard_error."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"llm refused reason={reason}")


class SpendCapExceeded(LLMRefused):
    def __init__(self) -> None:
        super().__init__("spend_cap")


class LLMProviderError(LLMError):
    """The provider call failed (after bounded retries). `status` is the HTTP
    status when there was one; `retry_after_s` is a 429's bounded Retry-After
    (code `rate_limited`), used by the guard's limiter cool-down."""

    def __init__(
        self, code: str, *, status: int | None = None, retry_after_s: float | None = None
    ) -> None:
        self.code = code
        self.status = status
        self.retry_after_s = retry_after_s
        super().__init__(
            f"llm provider error code={code}" + (f" status={status}" if status else "")
        )


class LLMResponseError(LLMError):
    """The provider answered but the answer is unusable (bad JSON, schema
    mismatch after the repair retry, malformed tool call)."""

    def __init__(self, code: str, *, usage: Usage | None = None) -> None:
        self.code = code
        # Provider-reported usage when the body parsed far enough to have it.
        self.usage = usage
        super().__init__(f"llm bad response code={code}")


class LLMAuditError(LLMError):
    """The audit row could not be written; the result is withheld (fail closed)."""

    def __init__(self) -> None:
        super().__init__("llm audit write failed")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolCallRequest(_Frozen):
    id: str
    name: str
    arguments: dict[str, Any]


class ToolSpec(_Frozen):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ChatMessage(_Frozen):
    """OpenAI-shaped message. Assistant messages may carry `tool_calls`;
    `tool` messages answer one via `tool_call_id`. `content` is str only."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str | None = None

    @field_validator("content")
    @classmethod
    def _no_nul(cls, v: str | None) -> str | None:
        return v.replace("\x00", "") if v is not None else None


class ChatRequest(_Frozen):
    purpose: Purpose
    lane: Lane
    messages: tuple[ChatMessage, ...] = Field(min_length=1)
    tools: tuple[ToolSpec, ...] = ()
    response_schema: type[BaseModel] | None = None
    max_output_tokens: int = Field(default=800, gt=0, le=32_000)
    temperature: float = Field(default=0.3, ge=0, le=2)
    turn_id: uuid.UUID | None = None

    @field_validator("tools")
    @classmethod
    def _tool_cap(cls, v: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]:
        if len(v) > MAX_TOOLS:
            raise ValueError("too many tools")
        return v


class Usage(_Frozen):
    tokens_in: int = 0
    tokens_out: int = 0


class ProviderResponse(_Frozen):
    """What a `Provider` returns: already parsed from the wire format."""

    text: str | None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: Usage = Usage()


class ChatResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    text: str | None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    parsed: BaseModel | None = None
    usage: Usage
    cost_inr: Decimal
    audit_id: uuid.UUID
