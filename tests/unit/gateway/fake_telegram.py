# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Fake Telegram HTTP boundary for gateway tests: an `httpx` transport double
mounted under the real `EgressClient`, so the real allowlist/guard still run.
No socket is ever opened. It mimics the Bot API semantics that matter:
`getUpdates(offset)` confirms (deletes) every update with a lower id."""

from __future__ import annotations

import json
from typing import Any

import httpx

from src.gateway import GatewaySettings
from src.net import EgressClient, NetSettings

TOKEN = "123456789:AAH_fake_token_for_tests_only_0123456789"
USER = 4242


def make_settings(**kw: Any) -> GatewaySettings:
    base: dict[str, Any] = {"bot_token": TOKEN, "allowed_user_id": USER}
    base.update(kw)
    return GatewaySettings(_env_file=None, **base)


def message(update_id: int, text: str = "hi", *, user: int = USER, chat: int | None = None) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": user, "is_bot": False},
            "chat": {"id": user if chat is None else chat, "type": "private"},
            "text": text,
        },
    }


def callback(update_id: int, data: str = "ap:xyz", *, user: int = USER) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": user},
            "data": data,
            "message": {"message_id": 7},
        },
    }


class FakeTelegram(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []  # unconfirmed server-side updates
        self.requests: list[tuple[str, dict[str, Any]]] = []  # (method, json body)
        self.sent: list[dict[str, Any]] = []
        self.raw_urls: list[str] = []
        self.scripted: list[httpx.Response] = []  # popped before normal handling
        self.files: dict[str, bytes] = {}
        self.file_sizes: dict[str, int] = {}
        self.next_message_id = 100
        self.poll_hook: Any = None  # called on each getUpdates

    def push(self, *updates: dict[str, Any]) -> None:
        self.pending.extend(updates)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.raw_urls.append(str(request.url))
        if self.scripted:
            return self.scripted.pop(0)
        prefix = f"/bot{TOKEN}/"
        fprefix = f"/file/bot{TOKEN}/"
        if path.startswith(fprefix):
            key = path[len(fprefix) :]
            return httpx.Response(200, content=self.files[key])
        if not path.startswith(prefix):
            return httpx.Response(404, json={"ok": False, "error_code": 404})
        method = path[len(prefix) :]
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.requests.append((method, body))
        if method == "getUpdates":
            if self.poll_hook is not None:
                self.poll_hook()
            off = body.get("offset")
            if off is not None:
                self.pending = [u for u in self.pending if u["update_id"] >= off]
            return httpx.Response(200, json={"ok": True, "result": list(self.pending[:100])})
        if method == "sendMessage":
            self.next_message_id += 1
            self.sent.append(body)
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": self.next_message_id}}
            )
        if method == "getFile":
            fid = body["file_id"]
            res: dict[str, Any] = {"file_id": fid, "file_path": f"docs/{fid}"}
            if fid in self.file_sizes:
                res["file_size"] = self.file_sizes[fid]
            self.files.setdefault(f"docs/{fid}", b"")
            return httpx.Response(200, json={"ok": True, "result": res})
        return httpx.Response(200, json={"ok": True, "result": True})

    def methods(self) -> list[str]:
        return [m for m, _ in self.requests]


def make_egress(fake: FakeTelegram) -> EgressClient:
    return EgressClient(NetSettings(_env_file=None), transport=fake)
