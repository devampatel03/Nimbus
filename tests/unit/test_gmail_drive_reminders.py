# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Gmail/Drive/Reminders tools (this session, item 6): unit tests for the
intent-builder pattern (gmail/drive route through the scrubber; reminders
never does), the gmail `send` dry-run, and the ALLOWED_TOOLS drift check
(item 6's "closes the tech debt item about drift").
"""

import uuid
from typing import Any

from src.contracts.tool_intent import RiskClass, ToolIntent, ToolResult
from src.security import scrubber
from src.tools.integrations.drive import DriveTool
from src.tools.integrations.gmail import GmailTool
from src.tools.integrations.reminders import RemindersTool
from src.tools.registry import ToolRegistry


def test_allowed_tools_matches_the_real_tool_registry():
    registry = ToolRegistry()
    assert set(scrubber.ALLOWED_TOOLS) == set(registry.names())


async def test_gmail_send_is_a_dry_run_and_never_calls_execute_external(monkeypatch):
    called = False

    async def _fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("send must not call execute_external while dry-run")

    monkeypatch.setattr(scrubber, "execute_external", _fail_if_called)
    monkeypatch.setattr("src.tools.integrations.gmail.execute_external", _fail_if_called)

    tool = GmailTool()
    result = await tool.run(
        "send",
        {"to": "someone@example.com", "subject": "hi", "body": "hello"},
        session_id=f"sess-{uuid.uuid4()}",
    )

    assert not called
    assert result.status == "dry_run"
    assert result.result["to"] == "someone@example.com"


async def test_gmail_read_search_draft_route_through_execute_external(monkeypatch):
    captured: list[ToolIntent] = []

    async def _fake_execute_external(intent: ToolIntent, session_id: str) -> ToolResult:
        captured.append(intent)
        return ToolResult(
            tool=intent.tool, action=intent.action, status="success", result={}, entity_refs={}
        )

    monkeypatch.setattr("src.tools.integrations.gmail.execute_external", _fake_execute_external)

    tool = GmailTool()
    await tool.run("read", {"message_id": "abc"}, session_id="s1")
    await tool.run("search", {"query": "invoice"}, session_id="s1")
    await tool.run("draft", {"to": "a@b.com", "subject": "s", "body": "b"}, session_id="s1")

    assert [i.action for i in captured] == ["read", "search", "draft"]
    assert captured[0].risk_class == RiskClass.READ
    assert captured[2].risk_class == RiskClass.WRITE_REVERSIBLE


async def test_drive_actions_route_through_execute_external(monkeypatch):
    captured: list[ToolIntent] = []

    async def _fake_execute_external(intent: ToolIntent, session_id: str) -> ToolResult:
        captured.append(intent)
        return ToolResult(
            tool=intent.tool, action=intent.action, status="success", result={}, entity_refs={}
        )

    monkeypatch.setattr("src.tools.integrations.drive.execute_external", _fake_execute_external)

    tool = DriveTool()
    await tool.run("list", {"folder_id": "root"}, session_id="s1")
    await tool.run("search", {"query": "report"}, session_id="s1")
    await tool.run("read", {"file_id": "f1"}, session_id="s1")

    assert all(i.risk_class == RiskClass.READ for i in captured)
    assert [i.action for i in captured] == ["list", "search", "read"]


async def test_reminders_never_touches_the_scrubber(monkeypatch):
    def _fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("reminders must never call execute_external")

    monkeypatch.setattr(scrubber, "execute_external", _fail_if_called)

    tool = RemindersTool()
    created = await tool.run(
        "create", {"title": "call mom", "remind_at": "2030-01-01T09:00:00"}, session_id="s1"
    )
    reminder_id = created.result["reminder_id"]
    assert isinstance(reminder_id, str)

    fetched = tool.get_reminder(reminder_id)
    assert fetched is not None
    assert fetched["title"] == "call mom"
    assert fetched["fired"] is False

    updated = await tool.run(
        "update",
        {"reminder_id": reminder_id, "title": "call dad", "remind_at": "2030-01-02T09:00:00"},
        session_id="s1",
    )
    assert updated.status == "success"
    assert tool.get_reminder(reminder_id)["title"] == "call dad"

    listed = await tool.run("list", {}, session_id="s1")
    assert listed.result == {"count": 1}

    deleted = await tool.run("delete", {"reminder_id": reminder_id}, session_id="s1")
    assert deleted.status == "success"
    assert tool.get_reminder(reminder_id) is None


async def test_reminders_get_and_delete_unknown_id_reports_not_found():
    tool = RemindersTool()
    result = await tool.run("get", {"reminder_id": "nope"}, session_id="s1")
    assert result.status == "error"
    assert result.error_message == "reminder not found"


async def test_reminders_fires_via_scheduler_without_a_real_wait():
    """Exercises the actual APScheduler job (not just the CRUD dict) by
    invoking the scheduled callback directly rather than sleeping in a test.
    """
    tool = RemindersTool()
    created = await tool.run(
        "create", {"title": "x", "remind_at": "2030-01-01T09:00:00"}, session_id="s1"
    )
    reminder_id = created.result["reminder_id"]
    assert isinstance(reminder_id, str)

    job = tool._scheduler.get_job(reminder_id)  # type: ignore[union-attr]
    assert job is not None

    tool._fire(reminder_id)
    assert tool.get_reminder(reminder_id)["fired"] is True
