# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.8: Drive tools. Content reaches the model only via ToolOutput.external."""

from __future__ import annotations

import json

import pytest

from src.lanes.data import UntrustedText
from src.orchestrator.dispatch import Risk, ToolError
from src.tools.integrations.drive_read import MAX_EXTERNAL_CHARS, DriveReadTool
from tests.unit.integrations.google.drive_helpers import (
    ID_BIG,
    ID_DOC,
    ID_INJECT,
    ID_PDF,
    FakeDrive,
    fx,
)


def _tools(drive: FakeDrive) -> dict[str, object]:
    g = DriveReadTool()
    g.bind(drive.reader())
    return {t.spec.name: t for t in g.tools()}


def _routes_inject() -> dict[str, tuple[int, bytes]]:
    return {
        f"/drive/v3/files/{ID_INJECT}": (200, fx("drive_meta_injected.json")),
        f"/drive/v3/files/{ID_INJECT}?media": (200, fx("drive_content_injected.txt")),
    }


def test_two_read_tools_with_strict_schemas() -> None:
    tools = DriveReadTool().tools()
    assert {t.spec.name for t in tools} == {"drive_search", "drive_read"}
    assert all(t.risk is Risk.READ for t in tools)
    for t in tools:
        assert t.spec.parameters["additionalProperties"] is False


async def test_unbound_fails_closed() -> None:
    for t in DriveReadTool().tools():
        with pytest.raises(ToolError, match="unavailable"):
            await t.run({k: "x" for k in t.spec.parameters["properties"]})


async def test_search_text_has_only_ids_and_metadata_names_go_external() -> None:
    drive = FakeDrive({"/drive/v3/files": (200, fx("drive_list.json"))})
    out = await _tools(drive)["drive_search"].run({"query": "budget"})  # type: ignore[attr-defined]
    body = json.loads(out.text)
    assert body["count"] == 3
    assert "Q3 budget" not in out.text and "scan.pdf" not in out.text
    assert "Test Owner" not in out.text and "example.invalid" not in out.text
    assert len(out.external) == 1 and isinstance(out.external[0], UntrustedText)
    assert out.untrusted is True
    assert [f["readable"] for f in body["files"]] == [True, False, True]
    assert body["files"][1]["mime_type"] == "other"


async def test_search_empty_has_no_external_item() -> None:
    drive = FakeDrive({"/drive/v3/files": (200, b'{"files": []}')})
    out = await _tools(drive)["drive_search"].run({"query": ""})  # type: ignore[attr-defined]
    assert json.loads(out.text)["count"] == 0 and out.external == ()


async def test_read_google_doc_content_only_in_external() -> None:
    drive = FakeDrive(
        {
            f"/drive/v3/files/{ID_DOC}": (200, fx("drive_meta_doc.json")),
            f"/drive/v3/files/{ID_DOC}/export": (200, b"SECRET-BODY line"),
        }
    )
    out = await _tools(drive)["drive_read"].run({"file_id": ID_DOC})  # type: ignore[attr-defined]
    assert "SECRET-BODY" not in out.text and "Q3 budget" not in out.text
    assert json.loads(out.text)["truncated"] is False
    assert len(out.external) == 1


async def test_read_clips_chars_and_reports_truncation() -> None:
    drive = FakeDrive(
        {
            f"/drive/v3/files/{ID_DOC}": (200, fx("drive_meta_doc.json")),
            f"/drive/v3/files/{ID_DOC}/export": (200, b"x" * (MAX_EXTERNAL_CHARS * 3)),
        }
    )
    out = await _tools(drive)["drive_read"].run({"file_id": ID_DOC})  # type: ignore[attr-defined]
    body = json.loads(out.text)
    assert body["truncated"] is True and body["chars"] == MAX_EXTERNAL_CHARS


async def test_binary_and_big_refused_with_clean_message() -> None:
    drive = FakeDrive(
        {
            f"/drive/v3/files/{ID_PDF}": (200, fx("drive_meta_pdf.json")),
            f"/drive/v3/files/{ID_BIG}": (200, fx("drive_meta_big.json")),
        }
    )
    t = _tools(drive)["drive_read"]
    with pytest.raises(ToolError, match="cannot be read as text"):
        await t.run({"file_id": ID_PDF})  # type: ignore[attr-defined]
    with pytest.raises(ToolError, match="too large"):
        await t.run({"file_id": ID_BIG})  # type: ignore[attr-defined]


async def test_invalid_id_and_unknown_args_rejected() -> None:
    drive = FakeDrive({})
    t = _tools(drive)["drive_read"]
    with pytest.raises(ToolError):
        await t.run({"file_id": "../../x"})  # type: ignore[attr-defined]
    with pytest.raises(ToolError):
        await t.run({"file_id": ID_DOC, "extra": 1})  # type: ignore[attr-defined]
    assert drive.requests == []


async def test_injected_document_stays_out_of_text_and_names() -> None:
    drive = FakeDrive(_routes_inject())
    out = await _tools(drive)["drive_read"].run({"file_id": ID_INJECT})  # type: ignore[attr-defined]
    low = out.text.lower()
    assert "ignore previous" not in low and "attacker" not in low and "share" not in low
    assert "gmail_send" not in low


async def test_api_failure_message_has_no_upstream_detail() -> None:
    drive = FakeDrive({f"/drive/v3/files/{ID_DOC}": (500, b"internal: token ya29.leak")})
    with pytest.raises(ToolError) as ei:
        await _tools(drive)["drive_read"].run({"file_id": ID_DOC})  # type: ignore[attr-defined]
    assert "ya29" not in str(ei.value)
