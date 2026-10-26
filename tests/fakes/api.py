# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""API-test builder: a real `Orchestrator` over a scripted `FakeLLM` under the
real `GuardedLLM`, with the interim `DenyAllWritesPolicy`, wired to a fresh
`ToolRegistry`. No model, no network."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import FastAPI

from src.api.main import create_app
from src.llm.fake import FakeLLM, Script
from src.orchestrator.dispatch import DenyAllWritesPolicy
from src.orchestrator.loop import Orchestrator
from src.tools.registry import ToolRegistry
from tests.fakes.llm import build_env


def make_app(script: Iterable[Script] = (), *, default: str = "Got it.") -> FastAPI:
    registry = ToolRegistry()
    env = build_env(provider=FakeLLM(list(script), default=default))
    orch = Orchestrator(
        env.llm,
        registry.loop_tools(),
        DenyAllWritesPolicy(),
        character_card="You are Nimbus.",
        nonce_factory=lambda: "n0nce",
    )
    return create_app(orchestrator=orch, registry=registry)
