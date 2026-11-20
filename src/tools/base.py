# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Tool protocol v2 (ADR-0006, P2.5b).

A tool, as the loop sees it, is a `LoopTool` (`src.orchestrator.dispatch`):
`spec` (name, description, JSON-schema parameters), a code-declared `risk`,
and `async run(args) -> ToolOutput`. Risk is a property of the tool, never of
model output, so a tool whose actions differ in risk is split into one tool per
action (least privilege; the policy sees `(tool, args, risk)` only).

`FunctionTool` is the small concrete implementation; it validates `args`
against the declared JSON schema (required keys, no extra keys, primitive
types) before the handler runs, and raises `ToolError` on any mismatch.
`ToolGroup` bundles the tools that share state (for example one task store)
and is what `ToolRegistry` discovers. A tool that needs a third-party service
must go through `src.net` (the only module allowed to import a transport,
INV-1). Tasks and reminders are purely local.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from src.llm.types import ToolSpec
from src.orchestrator.dispatch import LoopTool, Risk, ToolError, ToolOutput

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}

Handler = Callable[[Mapping[str, Any]], Awaitable[ToolOutput]]


def object_schema(
    properties: Mapping[str, str], required: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """JSON schema for a flat object of primitives. `properties` maps name to a
    JSON type name; all properties are required unless `required` says otherwise."""
    req = list(properties) if required is None else list(required)
    return {
        "type": "object",
        "properties": {k: {"type": v} for k, v in properties.items()},
        "required": req,
        "additionalProperties": False,
    }


def validate_args(parameters: Mapping[str, Any], args: Mapping[str, Any]) -> None:
    """Raise `ToolError` unless `args` fits the flat-object `parameters` schema."""
    if not isinstance(args, Mapping):
        raise ToolError("invalid arguments: expected an object")
    props: Mapping[str, Any] = parameters.get("properties", {})
    for key in args:
        if key not in props:
            raise ToolError("invalid arguments: unexpected parameter")
    for key in parameters.get("required", []):
        if key not in args:
            raise ToolError(f"invalid arguments: missing {key}")
    for key, value in args.items():
        expected = _JSON_TYPES.get(props[key].get("type", ""))
        if expected is None:
            raise ToolError("invalid arguments: unsupported schema type")
        # bool is an int subclass; reject it for integer/number, and None everywhere.
        if value is None or (isinstance(value, bool) and expected is not bool):
            raise ToolError(f"invalid arguments: {key} has the wrong type")
        if not isinstance(value, expected):
            raise ToolError(f"invalid arguments: {key} has the wrong type")


class FunctionTool:
    """A `LoopTool` over an async handler. `untrusted` defaults to True on the
    output the handler returns (see `ToolOutput`)."""

    def __init__(
        self,
        name: str,
        description: str,
        risk: Risk,
        parameters: dict[str, Any],
        handler: Handler,
        available: Callable[[], bool] | None = None,
    ) -> None:
        self.spec = ToolSpec(name=name, description=description, parameters=parameters)
        self.risk = risk
        self._handler = handler
        self._available = available

    def available(self) -> bool:
        """False while the tool's group is unbound (it would only fail closed).
        The loop does not offer unavailable tools to the model."""
        return True if self._available is None else bool(self._available())

    async def run(self, args: Mapping[str, Any]) -> ToolOutput:
        validate_args(self.spec.parameters, args)
        return await self._handler(args)


class ToolGroup(ABC):
    """Tools that share state. Concrete groups have a no-argument constructor so
    `ToolRegistry` can discover and instantiate them."""

    name: str

    @abstractmethod
    def tools(self) -> list[LoopTool]: ...

    @staticmethod
    def gate(tools: list[LoopTool], available: Callable[[], bool]) -> list[LoopTool]:
        """Mark every `FunctionTool` in `tools` as offered only while `available()`
        (typically "the group is bound"). Returns `tools`."""
        for t in tools:
            if isinstance(t, FunctionTool):
                t._available = available
        return tools
