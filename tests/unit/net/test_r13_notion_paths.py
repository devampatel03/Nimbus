# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""R13 (P3.7, ADR-0021): exactly four Notion calls are reachable (search, one
page's properties, a block's children read and append). Everything else is
denied before any transport call."""

from __future__ import annotations

import pytest

from src.net import EgressClient, EgressDenied, Service
from tests.unit.net.net_helpers import RecordingTransport, make_settings

ID = "0a1b2c3d-0000-4000-8000-0123456789ab"


def _client() -> tuple[EgressClient, RecordingTransport]:
    t = RecordingTransport()
    return EgressClient(make_settings(), transport=t), t


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/search"),
        ("GET", f"/v1/pages/{ID}"),
        ("GET", f"/v1/blocks/{ID}/children"),
        ("PATCH", f"/v1/blocks/{ID}/children"),
    ],
)
async def test_allowed_notion_requests_reach_transport(method: str, path: str) -> None:
    client, t = _client()
    async with client:
        resp = await client.request(Service.NOTION, method, path)
    assert resp.status_code == 200 and len(t.calls) == 1


@pytest.mark.parametrize(
    ("method", "path", "reason"),
    [
        ("DELETE", f"/v1/blocks/{ID}", "notion_path"),  # archive a block
        ("PATCH", f"/v1/blocks/{ID}", "notion_path"),  # update a block
        ("PATCH", f"/v1/pages/{ID}", "notion_method"),  # update / archive a page
        ("POST", "/v1/pages", "notion_path"),  # create a page
        ("POST", f"/v1/pages/{ID}", "notion_method"),
        ("DELETE", f"/v1/pages/{ID}", "notion_method"),
        ("GET", f"/v1/pages/{ID}/properties/title", "notion_path"),
        ("GET", "/v1/users", "notion_path"),
        ("GET", "/v1/users/me", "notion_path"),
        ("POST", "/v1/comments", "notion_path"),
        ("GET", "/v1/comments", "notion_path"),
        ("POST", f"/v1/databases/{ID}/query", "notion_path"),
        ("PATCH", f"/v1/databases/{ID}", "notion_path"),
        ("POST", "/v1/databases", "notion_path"),
        ("GET", "/v1/search", "notion_method"),
        ("PUT", "/v1/search", "notion_method"),
        ("POST", f"/v1/blocks/{ID}/children", "notion_method"),
        ("PUT", f"/v1/blocks/{ID}/children", "notion_method"),
        ("DELETE", f"/v1/blocks/{ID}/children", "notion_method"),
        ("GET", f"/v1/blocks/{ID}/children/", "notion_path"),
        ("GET", f"/v1/blocks/{ID}/children/extra", "notion_path"),
        ("GET", f"/v1/pages/{ID.upper()}", "notion_path"),
        ("GET", "/v1/pages/not-a-uuid", "notion_path"),
        ("GET", f"/v1/pages/{ID.replace('-', '')}", "notion_path"),
        ("GET", f"/V1/pages/{ID}", "notion_path"),
        ("POST", "/V1/search", "notion_path"),
        ("POST", "/v2/search", "notion_path"),
        ("POST", "/v1/search/", "notion_path"),
        ("GET", "/", "notion_path"),
        ("GET", f"/v1/pages/{ID}%2f..%2fusers", "bad_path"),
        ("GET", f"/v1/pages/{ID}/../users", "bad_path"),
        ("POST", "/gmail/v1/users/me/messages/send", "gmail_send_path"),
    ],
)
async def test_everything_else_is_denied_with_zero_transport_calls(
    method: str, path: str, reason: str
) -> None:
    client, t = _client()
    async with client:
        with pytest.raises(EgressDenied) as exc:
            await client.request(Service.NOTION, method, path)
    assert exc.value.reason == reason
    assert t.calls == []


async def test_notion_rule_does_not_touch_other_services() -> None:
    client, t = _client()
    async with client:
        await client.request(Service.TELEGRAM, "GET", "/v1/users/me")
    assert len(t.calls) == 1
