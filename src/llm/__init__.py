# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""The single `llm` module (INV-1, R4): the only importer of an LLM SDK.

Public surface: `GuardedLLM` (the `LLMClient` implementation), the request /
result types, `build_guarded_llm`, and the fakes in `src.llm.fake`.
Legacy `src.llm.client` stays importable for existing callers.
"""

from src.llm.factory import build_guarded_llm
from src.llm.guard import GuardedLLM
from src.llm.protocols import LLMClient
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    Lane,
    LLMMode,
    LLMRefused,
    Purpose,
    SpendCapExceeded,
    ToolCallRequest,
    ToolSpec,
    Usage,
)

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "GuardedLLM",
    "LLMClient",
    "LLMMode",
    "LLMRefused",
    "Lane",
    "Purpose",
    "SpendCapExceeded",
    "ToolCallRequest",
    "ToolSpec",
    "Usage",
    "build_guarded_llm",
]
