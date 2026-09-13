# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Tool registry: auto-discovers `BaseTool` subclasses under
`src/tools/integrations/` (T4.1 acceptance: "new tool discovered without
editing registration code").

Discovery walks the package with `pkgutil.iter_modules`, imports each
submodule, and collects every concrete `BaseTool` subclass found at
module scope. Dropping a new `some_tool.py` file with a `BaseTool`
subclass into `integrations/` is sufficient -- no import or registration
list elsewhere needs editing, which is exactly what
`tests/unit/test_tool_discovery.py` proves by writing a tool file into a
temp copy of the package at test time and asserting it appears.
"""

import importlib
import inspect
import pkgutil
from types import ModuleType

from src.tools import integrations
from src.tools.base import BaseTool


def _discover(package: ModuleType) -> dict[str, type[BaseTool]]:
    tools: dict[str, type[BaseTool]] = {}
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(
        package.__path__, prefix=f"{package.__name__}."
    ):
        module = importlib.import_module(module_name)
        for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseTool)
                and obj is not BaseTool
                and not inspect.isabstract(obj)
                and obj.__module__ == module.__name__
            ):
                tools[obj.name] = obj
    return tools


class ToolRegistry:
    def __init__(self, package: ModuleType = integrations) -> None:
        self._tool_classes = _discover(package)
        self._instances: dict[str, BaseTool] = {
            name: cls() for name, cls in self._tool_classes.items()
        }

    def refresh(self, package: ModuleType = integrations) -> None:
        """Re-scan the integrations package. Used by tests that add a
        tool file after the registry was first constructed.
        """
        importlib.invalidate_caches()
        self._tool_classes = _discover(package)
        self._instances = {name: cls() for name, cls in self._tool_classes.items()}

    def names(self) -> list[str]:
        return sorted(self._instances)

    def get(self, name: str) -> BaseTool:
        try:
            return self._instances[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._instances
