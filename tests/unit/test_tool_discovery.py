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
from src.orchestrator.dispatch import LoopTool, Risk, ToolOutput
from src.tools.base import FunctionTool, ToolGroup, object_schema


class WidgetTool(ToolGroup):
    name = "widget"

    async def _ping(self, args):
        return ToolOutput("pong")

    def tools(self) -> list[LoopTool]:
        return [FunctionTool("widget_ping", "test-only", Risk.READ, object_schema({}), self._ping)]
"""


def test_new_tool_discovered_without_editing_registration_code(tmp_path, monkeypatch) -> None:
    pkg_name = "_nimbus_test_tool_discovery_integrations"
    pkg_dir = tmp_path / pkg_name
    shutil.copytree(Path("src/tools/integrations"), pkg_dir)
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        package = importlib.import_module(pkg_name)
        registry = ToolRegistry(package=package)

        assert {"tasks", "reminders"} <= set(registry.names())
        assert "widget" not in registry.names()

        (pkg_dir / "widget.py").write_text(_WIDGET_TOOL_SOURCE, encoding="utf-8")
        registry.refresh(package=package)

        assert "widget" in registry.names()
        assert registry.get("widget").name == "widget"
        assert "widget_ping" in {t.spec.name for t in registry.loop_tools()}
    finally:
        for name in list(sys.modules):
            if name == pkg_name or name.startswith(pkg_name + "."):
                del sys.modules[name]


def test_real_integrations_package_discovers_only_local_tools() -> None:
    # P1.4c (M9): the calendar/gmail/drive intent builders are deleted; Google
    # tools return as real integrations in P3.x.
    registry = ToolRegistry()
    assert set(registry.names()) == {"tasks", "reminders"}
