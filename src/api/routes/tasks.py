# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""CRUD for the internal task store (`src.tools.integrations.tasks.TasksTool`).

Reuses the exact `TasksTool` instance the orchestrator's `act` node
executes tool calls against (`request.app.state.graph_deps.tool_registry`,
set once in `src.api.main.create_app`), so a task created via chat and a
task created via this REST surface land in the same store -- there is
only one "internal task store", not two.

Uses a fixed `session_id` ("rest-api") for calls into `TasksTool.run`:
`session_id` is a local routing/audit parameter (AD-12), not something
tied to who is calling -- Nimbus has exactly one user, and `TasksTool`
never routes through the scrubber's per-session entity-ref store anyway
(see `tasks.py`'s module docstring).
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.contracts.tool_intent import Primitive
from src.tools.integrations.tasks import TasksTool

_REST_SESSION_ID = "rest-api"

router = APIRouter()


class TaskCreate(BaseModel):
    title: str


class TaskUpdate(BaseModel):
    title: str


class Task(BaseModel):
    task_id: str
    title: str
    done: bool


def _to_task(raw: dict[str, Primitive]) -> Task:
    return Task(task_id=str(raw["task_id"]), title=str(raw["title"]), done=bool(raw["done"]))


def _get_tool(request: Request) -> TasksTool:
    tool = request.app.state.graph_deps.tool_registry.get("tasks")
    if not isinstance(tool, TasksTool):
        raise TypeError("registry did not return a TasksTool for 'tasks'")
    return tool


@router.get("", response_model=list[Task])
async def list_tasks(request: Request) -> list[Task]:
    return [_to_task(t) for t in _get_tool(request).list_tasks()]


@router.post("", response_model=Task, status_code=201)
async def create_task(payload: TaskCreate, request: Request) -> Task:
    tool = _get_tool(request)
    result = await tool.run("create", {"title": payload.title}, session_id=_REST_SESSION_ID)
    task_id = result.result["task_id"]
    if not isinstance(task_id, str):
        raise TypeError("create did not return a string task_id")
    task = tool.get_task(task_id)
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
    tool = _get_tool(request)
    result = await tool.run(
        "update", {"task_id": task_id, "title": payload.title}, session_id=_REST_SESSION_ID
    )
    if result.status == "error":
        raise HTTPException(status_code=404, detail=result.error_message)
    task = tool.get_task(task_id)
    if task is None:
        raise RuntimeError("task vanished immediately after update")
    return _to_task(task)


@router.post("/{task_id}/complete", response_model=Task)
async def complete_task(task_id: str, request: Request) -> Task:
    tool = _get_tool(request)
    result = await tool.run("complete", {"task_id": task_id}, session_id=_REST_SESSION_ID)
    if result.status == "error":
        raise HTTPException(status_code=404, detail=result.error_message)
    task = tool.get_task(task_id)
    if task is None:
        raise RuntimeError("task vanished immediately after complete")
    return _to_task(task)


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str, request: Request) -> None:
    tool = _get_tool(request)
    result = await tool.run("delete", {"task_id": task_id}, session_id=_REST_SESSION_ID)
    if result.status == "error":
        raise HTTPException(status_code=404, detail=result.error_message)
