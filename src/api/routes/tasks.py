# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""CRUD for the internal task store (`src.tools.integrations.tasks.TasksTool`).

Reads and writes go straight to the `TasksTool` group held by
`request.app.state.tool_registry`, the same instance whose `LoopTool`s the
orchestrator is built from, so there is one task store. REST writes are an
authenticated-owner surface, not model-proposed calls, so they do not pass
through the loop's `Policy`. P2.1b deletes this package.
"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.orchestrator.dispatch import ToolError
from src.tools.integrations.tasks import TasksTool

router = APIRouter()


class TaskCreate(BaseModel):
    title: str


class TaskUpdate(BaseModel):
    title: str


class Task(BaseModel):
    task_id: str
    title: str
    done: bool


def _to_task(raw: dict[str, str | bool]) -> Task:
    return Task(task_id=str(raw["task_id"]), title=str(raw["title"]), done=bool(raw["done"]))


def _get_tool(request: Request) -> TasksTool:
    tool = request.app.state.tool_registry.get("tasks")
    if not isinstance(tool, TasksTool):
        raise TypeError("registry did not return a TasksTool for 'tasks'")
    return tool


@router.get("", response_model=list[Task])
async def list_tasks(request: Request) -> list[Task]:
    return [_to_task(t) for t in _get_tool(request).list_tasks()]


async def _call(group: TasksTool, name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = next(t for t in group.tools() if t.spec.name == name)
    try:
        out = await tool.run(args)
    except ToolError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    parsed: dict[str, Any] = json.loads(out.text)
    return parsed


@router.post("", response_model=Task, status_code=201)
async def create_task(payload: TaskCreate, request: Request) -> Task:
    group = _get_tool(request)
    created = await _call(group, "tasks_create", {"title": payload.title})
    task = group.get_task(created["task_id"])
    if task is None:
        raise RuntimeError("task vanished immediately after create")
    return _to_task(task)


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str, request: Request) -> Task:
    task = _get_tool(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _to_task(task)


@router.put("/{task_id}", response_model=Task)
async def update_task(task_id: str, payload: TaskUpdate, request: Request) -> Task:
    group = _get_tool(request)
    await _call(group, "tasks_update", {"task_id": task_id, "title": payload.title})
    return _to_task(_must_get(group, task_id))


@router.post("/{task_id}/complete", response_model=Task)
async def complete_task(task_id: str, request: Request) -> Task:
    group = _get_tool(request)
    await _call(group, "tasks_complete", {"task_id": task_id})
    return _to_task(_must_get(group, task_id))


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str, request: Request) -> None:
    await _call(_get_tool(request), "tasks_delete", {"task_id": task_id})


def _must_get(group: TasksTool, task_id: str) -> dict[str, str | bool]:
    task = group.get_task(task_id)
    if task is None:
        raise RuntimeError("task vanished immediately after write")
    return task
