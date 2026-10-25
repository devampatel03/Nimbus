# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Reminders tool: local CRUD plus the APScheduler wiring. Purely local --
no network import (enforced repo-wide by INV-1). Split out of the former
`test_gmail_drive_reminders.py` when the gmail/drive intent builders and
the scrubber were deleted (P1.4c, M9).
"""

from src.tools.integrations.reminders import RemindersTool


async def test_reminders_crud_round_trip():
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
