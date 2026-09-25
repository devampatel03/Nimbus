# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""POST /chat: send a message, get a response."""

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import settings
from src.api.main import create_app
from src.orchestrator.graph import GraphDependencies
from tests.conftest import FakeLLMClient


def _app():
    return create_app(deps=GraphDependencies(llm_client=FakeLLMClient()))


async def _login(client: AsyncClient) -> dict[str, str]:
    response = await client.post("/auth/token", json={"password": settings.auth_password})
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_send_message_get_response() -> None:
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _login(client)
        response = await client.post("/chat", json={"message": "hello there"}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["response"]
    assert "session_id" in body
    assert body["tool_calls"] == []
    assert body["tool_results"] == []


@pytest.mark.asyncio
async def test_chat_and_rest_tasks_share_the_same_internal_task_store() -> None:
    """`act` executes tools via the same `TasksTool` instance the REST
    `tasks` router reads from (see AD-14, `src/api/routes/tasks.py`
    module docstring) -- a task created through chat must be visible via
    GET /tasks, not stuck in a second, disconnected store.
    """
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _login(client)
        chat_response = await client.post(
            "/chat", json={"message": "remind me to call the bank"}, headers=headers
        )
        assert chat_response.status_code == 200
        assert chat_response.json()["tool_results"][0]["status"] == "success"

        tasks_response = await client.get("/tasks", headers=headers)

    assert tasks_response.status_code == 200
    tasks = tasks_response.json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "remind me to call the bank"
