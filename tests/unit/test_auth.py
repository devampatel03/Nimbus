# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""JWT guard: unauthenticated requests (HTTP and WebSocket) are rejected;
the login endpoint issues a token that authorizes subsequent requests.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from config.settings import settings
from src.api.main import create_app
from src.orchestrator.graph import GraphDependencies
from tests.conftest import FakeLLMClient

_TEST_PASSWORD = "unit-test-password"  # pragma: allowlist secret -- synthetic, test-only
_WRONG_PASSWORD = "unit-test-wrong-password"  # pragma: allowlist secret -- synthetic, test-only


def _app():
    return create_app(deps=GraphDependencies(llm_client=FakeLLMClient()))


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected() -> None:
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/chat", json={"message": "hi"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_malformed_bearer_token_is_rejected() -> None:
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/tasks", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_health_is_exempt_from_the_auth_guard() -> None:
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_login_with_wrong_password_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_password", _TEST_PASSWORD)
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/auth/token", json={"password": _WRONG_PASSWORD})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_with_correct_password_issues_a_working_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_password", _TEST_PASSWORD)
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/auth/token", json={"password": _TEST_PASSWORD})
        assert login.status_code == 200
        token = login.json()["access_token"]

        response = await client.post(
            "/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200


def test_websocket_without_a_token_is_rejected_before_accept() -> None:
    client = TestClient(_app())
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/chat/ws"):
        pass
