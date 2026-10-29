# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-6 / R13 / INV-5 for the Notion tools (P3.7). New file (H14): nothing else
under tests/eval is touched.

* `ToolOutput.text` of every Notion tool carries only counts, opaque handles and
  fixed statuses; every Notion-authored string and every Notion id is absent from
  it and arrives only as `UntrustedText` in `ToolOutput.external`.
* The Notion code has no update/delete/archive/share path: the only HTTP verbs it
  names are GET, POST (search) and PATCH (append children).
* The Notion scope is not a local scope, so the default wiring cannot run it.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from src.core.turn_context import turn_event
from src.lanes.data import UntrustedText
from src.policy import LOCAL_SCOPES
from src.tools.integrations.notion import NotionTool
from tests.unit.integrations.notion.notion_helpers import P1, P2, FakeNotion, fx, ok

ROOT = Path(__file__).parents[3]
HOSTILE = (
    "Trip plan",
    "Ignore previous",
    "evil.example.invalid",
    "gmail_send",
    "Flights booked",
    "Hotel",
    "SYSTEM:",
    "attacker@example.invalid",
    "notion.so",
    P1,
    P2,
    P1.replace("-", ""),
)


def _tools(routes: dict[tuple[str, str], list[Any]]) -> dict[str, Any]:
    group = NotionTool()
    group.bind(FakeNotion(routes).client())
    return {t.spec.name: t for t in group.tools()}


async def test_tool_text_is_counts_handles_and_statuses_only() -> None:
    routes: dict[tuple[str, str], list[Any]] = {
        ("POST", "/v1/search"): [ok(fx("notion_search.json"))],
        ("GET", f"/v1/pages/{P1}"): [ok(fx("notion_page.json"))],
        ("GET", f"/v1/pages/{P2}"): [ok(fx("notion_page_injected.json"))],
        ("GET", f"/v1/blocks/{P1}/children"): [ok(fx("notion_children.json"))],
        ("GET", f"/v1/blocks/{P2}/children"): [ok(fx("notion_children_injected.json"))],
        ("PATCH", f"/v1/blocks/{P1}/children"): [ok({})],
    }
    tools = _tools(routes)
    search = await tools["notion_search"].run({"query": "x"})
    handles = json.loads(search.text)["pages"]
    outs = [
        search,
        await tools["notion_read"].run({"page": P1}),
        await tools["notion_read"].run({"page": P2}),
    ]
    with turn_event(uuid.UUID(int=3)):
        outs.append(await tools["notion_append"].run({"page": P1, "text": "note"}))
    assert handles and all(re.fullmatch(r"n[0-9a-f]{6}_\d+_\d+", h) for h in handles)
    for out in outs:
        for leak in HOSTILE:
            assert leak not in out.text, leak
        assert all(isinstance(u, UntrustedText) for u in out.external)
    assert sum(len(o.external) for o in outs[:3]) >= 4  # the content did arrive, as UntrustedText


def test_notion_code_names_no_mutating_verb_except_patch_append() -> None:
    src = "\n".join(
        (ROOT / rel).read_text(encoding="utf-8")
        for rel in ("src/integrations/notion/api.py", "src/tools/integrations/notion.py")
    )
    verbs = set(re.findall(r'"(GET|POST|PUT|PATCH|DELETE|HEAD)"', src))
    assert verbs == {"GET", "POST", "PATCH"}
    assert not re.search(r'f?"/v1/(?!search|pages/|blocks/)', src)
    assert src.count('"PATCH"') == 1 and "/children" in src


def test_notion_scope_is_not_local() -> None:
    assert "notion" not in LOCAL_SCOPES
