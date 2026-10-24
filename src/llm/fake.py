# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Fake `Provider`s for tests and CI (no network, no SDK).

They plug in under `GuardedLLM`, so tests exercise the real guard:

* `FakeLLM` -- scripted mode (a queue/callable of responses) and recorded
  mode (fixtures keyed by a stable request fingerprint, loadable from JSON).
* `CompromisedFakeLLM` -- simulates a fully prompt-injected model: it obeys
  any instruction found in the messages (calls any tool they name, echoes
  injected "say/reply with" text). Used by the red-team suites to assert that
  *no unauthorised effect* occurs even when the model complies.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from src.llm.types import ChatRequest, ProviderResponse, ToolCallRequest, Usage

Script = ProviderResponse | str | Exception | Callable[[ChatRequest], "ProviderResponse | str"]


class FixtureMiss(LookupError):
    """Recorded mode had no fixture for the request fingerprint."""

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        super().__init__(f"no recorded fixture for fingerprint={fingerprint}")


def fingerprint(req: ChatRequest) -> str:
    """Stable hash of what the model would see (purpose, messages, tool names)."""
    payload = {
        "purpose": req.purpose.value,
        "messages": [m.model_dump(mode="json") for m in req.messages],
        "tools": [t.name for t in req.tools],
    }
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _coerce(item: ProviderResponse | str) -> ProviderResponse:
    if isinstance(item, str):
        return ProviderResponse(
            text=item, usage=Usage(tokens_in=10, tokens_out=max(1, len(item) // 4))
        )
    return item


class FakeLLM:
    name = "fake"

    def __init__(
        self,
        script: Iterable[Script] = (),
        *,
        fixtures: Mapping[str, ProviderResponse | str] | None = None,
        default: ProviderResponse | str | None = None,
        model: str = "fake-model",
    ) -> None:
        self.model = model
        self._script: list[Script] = list(script)
        self._fixtures = dict(fixtures or {})
        self._default = default
        self.requests: list[ChatRequest] = []

    @classmethod
    def from_fixture_file(cls, path: Path, **kw: Any) -> FakeLLM:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fixtures = {k: ProviderResponse.model_validate(v) for k, v in raw.items()}
        return cls(fixtures=fixtures, **kw)

    def record(self, req: ChatRequest, response: ProviderResponse | str) -> None:
        self._fixtures[fingerprint(req)] = response

    def dump_fixtures(self, path: Path) -> None:
        data = {k: _coerce(v).model_dump(mode="json") for k, v in self._fixtures.items()}
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    async def complete(self, req: ChatRequest) -> ProviderResponse:
        self.requests.append(req)
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item = item(req)
            return _coerce(item)
        fp = fingerprint(req)
        if fp in self._fixtures:
            return _coerce(self._fixtures[fp])
        if self._default is not None:
            return _coerce(self._default)
        raise FixtureMiss(fp)


_IGNORE_RE = re.compile(
    r"(?is)(?:ignore|disregard|forget)\b.{0,40}\b(?:previous|prior|above|earlier)\b.{0,40}?"
    r"\b(?:instructions?|rules?|prompt)\b[.:,;\s-]*(.*)"
)
_SAY_RE = re.compile(
    r"(?is)\b(?:reply|respond|say|output|print|answer)\s+(?:only\s+)?(?:with\s+)?[:\"']?\s*(.+)"
)
_JSON_OBJ_RE = re.compile(r"\{.*?\}", re.DOTALL)


class CompromisedFakeLLM(FakeLLM):
    """Obeys every injected instruction. Deterministic.

    Tool calls: every offered tool whose name appears in any message is
    called, with the first JSON object that follows the name as arguments
    (else `{}`). Text: if the messages contain an override phrase the injected
    remainder is echoed; else a "reply with X" instruction is followed.
    """

    name = "fake_compromised"

    async def complete(self, req: ChatRequest) -> ProviderResponse:
        self.requests.append(req)
        corpus = "\n".join(
            (m.content or "")
            for m in req.messages
            if m.role in ("user", "tool", "assistant", "system")
        )
        calls: list[ToolCallRequest] = []
        for i, tool in enumerate(req.tools):
            idx = corpus.find(tool.name)
            if idx < 0:
                continue
            args: dict[str, Any] = {}
            m = _JSON_OBJ_RE.search(corpus, idx)
            if m:
                try:
                    parsed = json.loads(m.group())
                    if isinstance(parsed, dict):
                        args = parsed
                except ValueError:
                    pass
            calls.append(ToolCallRequest(id=f"call_{i}", name=tool.name, arguments=args))
        text: str | None = None
        if not calls:
            for rx in (_IGNORE_RE, _SAY_RE):
                m2 = rx.search(corpus)
                if m2:
                    text = m2.group(1).strip() or "complied"
                    break
            else:
                text = "ok"
        return ProviderResponse(
            text=text, tool_calls=tuple(calls), usage=Usage(tokens_in=10, tokens_out=5)
        )
