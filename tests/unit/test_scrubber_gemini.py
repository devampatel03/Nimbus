# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""This session: execute_external now makes a real Gemini generateContent
call. These tests exercise the request shape, response parsing, error
handling, and token-count logging against a fake httpx.AsyncClient.post --
no real network call, no real API key needed to verify the wiring is
correct (a live call is a manual/ops concern, not a unit test).
"""

import uuid
from typing import Any

import httpx
import pytest

from config.settings import settings
from src.contracts.tool_intent import RiskClass, ToolIntent
from src.security import kill_switch, scrubber


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    kill_switch.disengage()
    yield
    kill_switch.disengage()


def _intent() -> ToolIntent:
    return ToolIntent(
        tool="calendar",
        action="list",
        params={"start": "2026-01-01", "end": "2026-01-02"},
        entity_refs={},
        risk_class=RiskClass.READ,
    )


def _gemini_response(body: dict[str, Any], *, usage: dict[str, int] | None = None) -> dict:
    import json as _json

    return {
        "candidates": [{"content": {"parts": [{"text": _json.dumps(body)}]}}],
        "usageMetadata": usage
        or {"promptTokenCount": 12, "candidatesTokenCount": 4, "totalTokenCount": 16},
    }


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


async def test_execute_external_calls_gemini_generate_content_with_expected_shape(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_post(self, url, *, json, headers):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(_gemini_response({"count": 3}))

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    result = await scrubber.execute_external(_intent(), session_id=f"sess-{uuid.uuid4()}")

    assert captured["url"] == f"{settings.cloud_egress_url}/{settings.gemini_model}:generateContent"
    assert captured["headers"]["x-goog-api-key"] == settings.cloud_egress_api_key
    assert captured["json"]["generationConfig"]["responseMimeType"] == "application/json"
    # The prompt content is the scrubbed ToolIntent JSON, nothing else (INV-2).
    prompt_text = captured["json"]["contents"][0]["parts"][0]["text"]
    assert '"tool": "calendar"' in prompt_text or '"tool":"calendar"' in prompt_text
    assert result.status == "success"
    assert result.result == {"count": 3}


async def test_execute_external_logs_gemini_token_counts(monkeypatch, caplog):
    async def _fake_post(self, url, *, json, headers):
        return _FakeResponse(
            _gemini_response(
                {"count": 1},
                usage={"promptTokenCount": 42, "candidatesTokenCount": 7, "totalTokenCount": 49},
            )
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    with caplog.at_level("INFO", logger="src.security.scrubber"):
        await scrubber.execute_external(_intent(), session_id=f"sess-{uuid.uuid4()}")

    assert any("42" in r.message and "49" in r.message for r in caplog.records)


async def test_execute_external_rejects_non_json_response(monkeypatch):
    async def _fake_post(self, url, *, json, headers):
        return _FakeResponse({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    with pytest.raises(scrubber.ExternalCallError):
        await scrubber.execute_external(_intent(), session_id=f"sess-{uuid.uuid4()}")


async def test_execute_external_rejects_response_with_no_candidates(monkeypatch):
    async def _fake_post(self, url, *, json, headers):
        return _FakeResponse({"candidates": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    with pytest.raises(scrubber.ExternalCallError):
        await scrubber.execute_external(_intent(), session_id=f"sess-{uuid.uuid4()}")


async def test_execute_external_rejects_non_primitive_response_value(monkeypatch):
    async def _fake_post(self, url, *, json, headers):
        return _FakeResponse(_gemini_response({"nested": {"a": 1}}))

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    with pytest.raises(scrubber.ExternalCallError):
        await scrubber.execute_external(_intent(), session_id=f"sess-{uuid.uuid4()}")


async def test_execute_external_still_uses_x_goog_api_key_header_not_bearer(monkeypatch):
    """Regression guard: the previous generic stub sent an Authorization:
    Bearer header, which the real Gemini API does not accept.
    """
    captured_headers: dict[str, Any] = {}

    async def _fake_post(self, url, *, json, headers):
        captured_headers.update(headers)
        return _FakeResponse(_gemini_response({}))

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    await scrubber.execute_external(_intent(), session_id=f"sess-{uuid.uuid4()}")

    assert "Authorization" not in captured_headers
    assert "x-goog-api-key" in captured_headers
