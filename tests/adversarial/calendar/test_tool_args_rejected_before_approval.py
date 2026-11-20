# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""#39 LOW-D: a stronger notification-args test. A compromised model adds
parameters the tool schema does not have (`sendUpdates`, `conferenceDataVersion`,
`sendNotifications`, `attendees`, ...) to a Calendar or Notion write. Before,
the old test only checked "no Google call", which held trivially because a
write waits for approval; the extra keys still reached the approval card.

Now the tool's `resolve_args` validates against the schema BEFORE the policy
and the card: the call ends as `tool.args` (audited, `executed=false`), no card
is sent, and no request leaves. The execute path (an approval that somehow
carries an extra key) also refuses before any I/O."""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.approvals.types import AskOutcome
from src.integrations.google.calendar_api import CalendarClient
from src.net import EgressClient
from src.orchestrator.dispatch import Dispatcher, ToolError
from src.policy import LOCAL_SCOPES, PolicyEngine
from src.tools.integrations.google_calendar import GoogleCalendarTool
from src.tools.integrations.notion import NotionTool
from tests.fakes.clock import FakeClock
from tests.unit.integrations.notion.notion_helpers import FakeNotion, fx, ok
from tests.unit.net.net_helpers import RecordingTransport, make_settings
from tests.unit.tools.test_google_calendar import NOW, StubAuth

CREATE = {"title": "x", "start": "2026-11-01T09:00:00+05:30", "end": "2026-11-01T10:00:00+05:30"}
EXTRAS: list[tuple[str, Any]] = [
    ("sendUpdates", "all"),
    ("sendUpdates", "none"),
    ("conferenceDataVersion", 1),
    ("sendNotifications", True),
    ("attendees", "attacker@evil.example"),
    ("SENDUPDATES", "all"),
    ("send_updates", "all"),
    ("title\u200b", "x"),
]
WRONG_TYPES: list[dict[str, Any]] = [
    {**CREATE, "title": 5},
    {**CREATE, "start": None},
    {**CREATE, "description": ["a"]},
    {"title": "x", "start": "2026-11-01T09:00:00+05:30"},  # missing end
]


class Requester:
    def __init__(self) -> None:
        self.asks: list[Any] = []

    async def request(self, ask: Any) -> AskOutcome:
        self.asks.append(ask)
        return AskOutcome.REQUESTED


class Audit:
    def __init__(self) -> None:
        self.recs: list[Any] = []

    async def audit(self, rec: Any) -> None:
        self.recs.append(rec)


def calendar() -> tuple[dict[str, Any], RecordingTransport]:
    transport = RecordingTransport()
    group = GoogleCalendarTool("123456")
    group.bind(
        CalendarClient(EgressClient(make_settings(), transport=transport), StubAuth()),  # type: ignore[arg-type]
        FakeClock(NOW),
    )
    return {t.spec.name: t for t in group.tools()}, transport


async def _dispatch(tools: dict[str, Any], scope: str, name: str, args: dict[str, Any]) -> Any:
    req, audit = Requester(), Audit()
    d = Dispatcher(
        tools,
        PolicyEngine(),
        scopes_enabled=LOCAL_SCOPES | {scope},
        approvals=req,
        audit=audit,
    )
    r = await d.dispatch(name, args, exposed=frozenset(tools))
    return r, req, audit


@pytest.mark.parametrize(("key", "value"), EXTRAS)
async def test_calendar_create_extra_param_is_rejected_before_the_card(
    key: str, value: Any
) -> None:
    tools, transport = calendar()
    r, req, audit = await _dispatch(tools, "calendar", "calendar_create", {**CREATE, key: value})
    assert r.rule_id == "tool.args" and r.status == "error" and not r.needs_approval
    assert req.asks == [] and transport.calls == []
    (rec,) = audit.recs
    assert (rec.event, rec.executed) == ("args_rejected", False)


@pytest.mark.parametrize("args", WRONG_TYPES)
async def test_calendar_create_wrong_types_are_rejected_before_the_card(
    args: dict[str, Any],
) -> None:
    tools, transport = calendar()
    r, req, _ = await _dispatch(tools, "calendar", "calendar_create", args)
    assert r.rule_id == "tool.args" and req.asks == [] and transport.calls == []


async def test_clean_create_still_reaches_the_card() -> None:
    tools, transport = calendar()
    r, req, _ = await _dispatch(tools, "calendar", "calendar_create", dict(CREATE))
    assert r.needs_approval and len(req.asks) == 1 and transport.calls == []


@pytest.mark.parametrize(("key", "value"), EXTRAS[:4])
async def test_execute_path_with_extra_param_makes_no_request(key: str, value: Any) -> None:
    tools, transport = calendar()
    with pytest.raises(ToolError):
        await tools["calendar_create"].run({**CREATE, key: value})
    assert transport.calls == []


async def test_notion_append_extra_param_is_rejected_before_the_card() -> None:
    page_search = ("POST", "/v1/search")
    notion = FakeNotion({page_search: [ok(fx("notion_search.json"))]})
    group = NotionTool()
    group.bind(notion.client())
    tools = {t.spec.name: t for t in group.tools()}
    handle = json.loads((await tools["notion_search"].run({"query": ""})).text)["pages"][0]
    notion.requests.clear()
    r, req, _ = await _dispatch(
        tools, "notion", "notion_append", {"page": handle, "text": "hi", "archived": True}
    )
    assert r.rule_id == "tool.args" and req.asks == [] and notion.requests == []
