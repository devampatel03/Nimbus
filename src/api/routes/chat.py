# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""POST /chat (single turn) and WS /chat/ws (multi-turn over one connection),
both driven by `request.app.state.orchestrator` (the plain tool loop,
`src.orchestrator.loop.Orchestrator`). When the app was built without an
orchestrator (the interim default), chat answers 503 rather than guessing.

A tool call in the response is reported as (tool, status, rule_id) only; tool
output is not returned to the client. Writes are denied by the interim policy.
P2.1b deletes this package.

Auth: both routes sit behind `src.api.middleware.auth.JWTAuthMiddleware`,
applied ahead of routing, so an unauthenticated WS handshake never reaches
`chat_ws` (rejected at the ASGI layer before `websocket.accept`).
"""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from src.llm.types import ChatMessage
from src.orchestrator.loop import Orchestrator, TurnResult

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ToolEvent(BaseModel):
    tool: str
    status: str
    rule_id: str


class ChatResponse(BaseModel):
    session_id: str
    response: str
    status: str
    tool_events: list[ToolEvent] = []


def _events(result: TurnResult) -> list[ToolEvent]:
    return [ToolEvent(tool=e.tool, status=e.status, rule_id=e.rule_id) for e in result.tool_events]


def _history(result: TurnResult) -> list[ChatMessage]:
    kept = [m for m in result.messages if m.role != "system"]
    return [*kept, ChatMessage(role="assistant", content=result.text)]


def _orchestrator(app_state: Any) -> Orchestrator | None:
    orch = getattr(app_state, "orchestrator", None)
    return orch if isinstance(orch, Orchestrator) else None


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    orch = _orchestrator(request.app.state)
    if orch is None:
        raise HTTPException(status_code=503, detail="chat is not available")
    session_id = payload.session_id or str(uuid.uuid4())
    result = await orch.run_turn(payload.message)
    return ChatResponse(
        session_id=session_id,
        response=result.text,
        status=result.status,
        tool_events=_events(result),
    )


@router.websocket("/ws")
async def chat_ws(websocket: WebSocket) -> None:
    orch = _orchestrator(websocket.app.state)
    if orch is None:
        await websocket.close(code=1013)
        return
    await websocket.accept()
    session_id = websocket.query_params.get("session_id") or str(uuid.uuid4())
    history: list[ChatMessage] = []
    try:
        while True:
            payload = await websocket.receive_json()
            message = str(payload.get("message", ""))
            result = await orch.run_turn(message, history=history)
            history = _history(result)
            await websocket.send_json(
                {
                    "type": "response",
                    "session_id": session_id,
                    "response": result.text,
                    "status": result.status,
                    "tool_events": [e.model_dump() for e in _events(result)],
                }
            )
    except WebSocketDisconnect:
        pass
