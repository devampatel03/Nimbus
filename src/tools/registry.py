# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Tool registry: auto-discovers `ToolGroup` subclasses under
`src/tools/integrations/` (T4.1 acceptance: "new tool discovered without
editing registration code").

Discovery walks the package with `pkgutil.iter_modules`, imports each
submodule, and collects every concrete `ToolGroup` subclass defined at module
scope. Dropping a new file with a `ToolGroup` subclass into `integrations/` is
sufficient; `tests/unit/test_tool_discovery.py` proves it. `loop_tools()` is the
flat list of `LoopTool`s the orchestrator loop is built from.
"""

import importlib
import inspect
import pkgutil
from types import ModuleType

from src.orchestrator.dispatch import LoopTool
from src.tools import integrations
from src.tools.base import ToolGroup


def _discover(package: ModuleType) -> dict[str, type[ToolGroup]]:
    groups: dict[str, type[ToolGroup]] = {}
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(
        package.__path__, prefix=f"{package.__name__}."
    ):
        module = importlib.import_module(module_name)
        for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, ToolGroup)
                and obj is not ToolGroup
                and not inspect.isabstract(obj)
                and obj.__module__ == module.__name__
            ):
                groups[obj.name] = obj
    return groups


class ToolRegistry:
    def __init__(self, package: ModuleType = integrations) -> None:
        self._classes = _discover(package)
        self._instances: dict[str, ToolGroup] = {n: c() for n, c in self._classes.items()}

    def refresh(self, package: ModuleType = integrations) -> None:
        """Re-scan the integrations package (tests add a file after first scan)."""
        importlib.invalidate_caches()
        self._classes = _discover(package)
        self._instances = {n: c() for n, c in self._classes.items()}

    def names(self) -> list[str]:
        return sorted(self._instances)

    def get(self, name: str) -> ToolGroup:
        try:
            return self._instances[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name!r}") from None

    def loop_tools(self) -> list[LoopTool]:
        return [t for name in self.names() for t in self._instances[name].tools()]

    def __contains__(self, name: str) -> bool:
        return name in self._instances
