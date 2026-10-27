# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Persisted tasks (P3.3): the tool group over Postgres; survives a new instance."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.orchestrator.dispatch import ToolError
from src.tools.integrations.tasks import TasksTool
from tests.fakes.clock import FakeClock

SF = async_sessionmaker[AsyncSession]


def group(sf: SF, clock: FakeClock) -> tuple[TasksTool, dict[str, Any]]:
    g = TasksTool()
    g.bind(sf, clock)
    return g, {t.spec.name: t for t in g.tools()}


async def test_tasks_crud_round_trip_and_survives_restart(sf: SF, clock: FakeClock) -> None:
    _, tools = group(sf, clock)
    created = await tools["tasks_create"].run({"title": "buy milk"})
    tid = json.loads(created.text)["task_id"]
    await tools["tasks_update"].run({"task_id": tid, "title": "buy oat milk"})
    await tools["tasks_complete"].run({"task_id": tid})

    _, tools2 = group(sf, clock)  # "restart": a new group instance, same database
    got = json.loads((await tools2["tasks_get"].run({"task_id": tid})).text)
    assert got == {"task_id": tid, "title": "buy oat milk", "done": True}
    listed = json.loads((await tools2["tasks_list"].run({})).text)
    assert listed["count"] == 1
    await tools2["tasks_delete"].run({"task_id": tid})
    assert json.loads((await tools2["tasks_list"].run({})).text)["count"] == 0


@pytest.mark.parametrize("tid", ["nope", "00000000-0000-0000-0000-000000000000"])
async def test_not_found_is_a_tool_error(sf: SF, clock: FakeClock, tid: str) -> None:
    _, tools = group(sf, clock)
    with pytest.raises(ToolError, match="task not found"):
        await tools["tasks_get"].run({"task_id": tid})


@pytest.mark.parametrize("title", ["", "  ", "x" * 501])
async def test_bad_titles_rejected(sf: SF, clock: FakeClock, title: str) -> None:
    _, tools = group(sf, clock)
    with pytest.raises(ToolError):
        await tools["tasks_create"].run({"title": title})
    assert json.loads((await tools["tasks_list"].run({})).text)["count"] == 0


async def test_unbound_group_fails_closed() -> None:
    tools = {t.spec.name: t for t in TasksTool().tools()}
    with pytest.raises(ToolError, match="unavailable"):
        await tools["tasks_list"].run({})
