# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""T4.1: a new tool file dropped into `src/tools/integrations/` is
discovered without editing any registration code. Proven by copying the
real `integrations` package onto disk under `tmp_path`, importing that
copy as a real, disk-backed package (not the live `src/tools/integrations`
-- this test must not mutate the repo), then writing a brand-new tool
module into it after the registry's first scan and calling `refresh()`.
"""

import importlib
import shutil
import sys
from pathlib import Path

from src.tools.registry import ToolRegistry

_WIDGET_TOOL_SOURCE = """
from typing import ClassVar

from src.contracts.tool_intent import Primitive, RiskClass, ToolResult
from src.tools.base import BaseTool


class WidgetTool(BaseTool):
    name = "widget"
    description = "test-only tool for discovery verification"
    risk_class = RiskClass.READ
    schema: ClassVar[dict[str, dict[str, type]]] = {"list": {}}

    async def run(self, action: str, params: dict, *, session_id: str) -> ToolResult:
        return ToolResult(tool=self.name, action=action, status="success", result={}, entity_refs={})
"""


def test_new_tool_discovered_without_editing_registration_code(tmp_path, monkeypatch) -> None:
    pkg_name = "_nimbus_test_tool_discovery_integrations"
    pkg_dir = tmp_path / pkg_name
    shutil.copytree(Path("src/tools/integrations"), pkg_dir)
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        package = importlib.import_module(pkg_name)
        registry = ToolRegistry(package=package)

        assert {"calendar", "tasks"} <= set(registry.names())
        assert "widget" not in registry.names()

        (pkg_dir / "widget.py").write_text(_WIDGET_TOOL_SOURCE, encoding="utf-8")
        registry.refresh(package=package)

        assert "widget" in registry.names()
        widget = registry.get("widget")
        assert widget.name == "widget"
    finally:
        for name in list(sys.modules):
            if name == pkg_name or name.startswith(pkg_name + "."):
                del sys.modules[name]


def test_real_integrations_package_discovers_calendar_and_tasks() -> None:
    registry = ToolRegistry()
    assert set(registry.names()) == {"calendar", "tasks"}
