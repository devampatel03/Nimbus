# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""WS /chat/ws: a streaming conversation works over one connection.

Uses `starlette.testclient.TestClient` (sync) rather than
`httpx.ASGITransport`, since plain `httpx` has no WebSocket support --
this is the standard way to exercise a Starlette/FastAPI WebSocket route
without a live server.
"""

from starlette.testclient import TestClient

from config.settings import settings
from tests.fakes.api import make_app


def _app():
    return make_app()


def _login(client: TestClient) -> str:
    response = client.post("/auth/token", json={"password": settings.auth_password})
    token = response.json()["access_token"]
    assert isinstance(token, str)
    return token


def test_streaming_conversation_multiple_turns_over_one_connection() -> None:
    client = TestClient(make_app(["first answer", "second answer"]))
    token = _login(client)

    with client.websocket_connect(f"/chat/ws?token={token}") as websocket:
        websocket.send_json({"message": "hello"})
        first = websocket.receive_json()
        assert first["type"] == "response"
        assert first["response"] == "first answer"
        session_id = first["session_id"]

        websocket.send_json({"message": "and again"})
        second = websocket.receive_json()

    assert second["session_id"] == session_id
    assert second["response"] == "second answer"
    assert second["tool_events"] == []


def test_websocket_accepts_a_token_via_authorization_header_too() -> None:
    client = TestClient(_app())
    token = _login(client)

    with client.websocket_connect(
        "/chat/ws", headers={"authorization": f"Bearer {token}"}
    ) as websocket:
        websocket.send_json({"message": "hello"})
        response = websocket.receive_json()

    assert response["type"] == "response"
