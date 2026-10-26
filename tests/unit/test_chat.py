# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""POST /chat: send a message, get a response."""

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import settings
from src.api.main import create_app
from src.llm.types import ProviderResponse, ToolCallRequest, Usage
from tests.fakes.api import make_app


def _app():
    return make_app()


async def _login(client: AsyncClient) -> dict[str, str]:
    response = await client.post("/auth/token", json={"password": settings.auth_password})
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_send_message_get_response() -> None:
    transport = ASGITransport(app=make_app(["Hello there."]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _login(client)
        response = await client.post("/chat", json={"message": "hello there"}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Hello there."
    assert body["status"] == "ok"
    assert "session_id" in body
    assert body["tool_events"] == []


@pytest.mark.asyncio
async def test_chat_without_an_orchestrator_is_503() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _login(client)
        response = await client.post("/chat", json={"message": "hi"}, headers=headers)
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_write_tool_call_from_chat_is_denied_by_policy_and_store_stays_empty() -> None:
    """The model proposes `tasks_create`; the interim DenyAllWritesPolicy denies
    it (INV-5), so nothing lands in the shared task store."""
    call = ProviderResponse(
        text=None,
        tool_calls=(ToolCallRequest(id="c1", name="tasks_create", arguments={"title": "x"}),),
        usage=Usage(tokens_in=10, tokens_out=5),
    )
    transport = ASGITransport(app=make_app([call, "I can't do that yet."]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _login(client)
        chat_response = await client.post(
            "/chat", json={"message": "add a task called x: tasks create"}, headers=headers
        )
        assert chat_response.status_code == 200
        events = chat_response.json()["tool_events"]
        assert events == [
            {"tool": "tasks_create", "status": "denied", "rule_id": "deny.write_interim"}
        ]
        tasks_response = await client.get("/tasks", headers=headers)

    assert tasks_response.status_code == 200
    assert tasks_response.json() == []


@pytest.mark.asyncio
async def test_rest_tasks_roundtrip_uses_loop_tools() -> None:
    transport = ASGITransport(app=make_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _login(client)
        created = await client.post("/tasks", json={"title": "call the bank"}, headers=headers)
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        done = await client.post(f"/tasks/{task_id}/complete", headers=headers)
        assert done.json()["done"] is True
        missing = await client.put("/tasks/nope", json={"title": "y"}, headers=headers)
        assert missing.status_code == 404
        assert (await client.delete(f"/tasks/{task_id}", headers=headers)).status_code == 204
