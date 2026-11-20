# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

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
    assert len(out.external) == 3 and all(isinstance(u, UntrustedText) for u in out.external)
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


async def test_drive_tools_offered_only_when_bound_and_scope_enabled() -> None:
    from src.llm.fake import FakeLLM
    from src.orchestrator.loop import Orchestrator
    from src.policy import PolicyEngine
    from tests.fakes.llm import build_env

    async def offered(group: DriveReadTool, **kw: object) -> set[str]:
        fake = FakeLLM(["ok"])
        o = Orchestrator(
            build_env(provider=fake).llm,
            group.tools(),
            PolicyEngine(),
            character_card="c",
            **kw,  # type: ignore[arg-type]
        )
        await o.run_turn("find my drive file")
        return {t.name for t in fake.requests[0].tools}

    unbound = DriveReadTool()
    bound = DriveReadTool()
    bound.bind(FakeDrive({}).reader())
    names = {"drive_search", "drive_read"}
    enabled = frozenset({"tasks", "reminders", "drive"})
    assert await offered(bound) == set()  # default wiring: local scopes only
    assert await offered(unbound, scopes_enabled=enabled) == set()  # unbound
    assert await offered(bound, scopes_enabled=enabled) == names


HOSTILE = ("ignore previous", "share this folder", "attacker", "system:", "boss")


async def test_hostile_metadata_never_reaches_output_text() -> None:
    drive = FakeDrive({"/drive/v3/files": (200, fx("drive_list_hostile.json"))})
    out = await _tools(drive)["drive_search"].run({"query": ""})  # type: ignore[attr-defined]
    low = out.text.lower()
    assert not [h for h in HOSTILE if h in low]
    body = json.loads(out.text)
    # the entry with a non-id `id` is dropped; hostile mime/time/size collapse to safe values
    assert body["count"] == 3
    ids = [f["file_id"] for f in body["files"]]
    assert all(i.startswith("1Hostile") for i in ids)
    assert body["files"][0]["modified"] == "" and body["files"][0]["size_bytes"] is None
    assert body["files"][1]["mime_type"] == "other" and body["files"][1]["readable"] is False
    assert body["files"][2]["mime_type"] == "other" and body["files"][2]["modified"] != ""


async def test_names_and_owners_are_single_line_and_one_item_per_file() -> None:
    drive = FakeDrive({"/drive/v3/files": (200, fx("drive_list_hostile.json"))})
    files = await drive.reader().search("")
    for f in files:
        for text in (f.name, *f.owners):
            assert not any(c in text for c in "\n\r\t\u200b\u202e\u2028")
    assert files[0].name.startswith("a.txt 2. Q3 plan")  # newline became a space
    out = await _tools(drive)["drive_search"].run({"query": ""})  # type: ignore[attr-defined]
    assert len(out.external) == len(files) == 3


def test_list_cap_never_exceeds_the_per_output_quarantine_cap() -> None:
    from src.integrations.google.drive_api import MAX_LIST_RESULTS
    from src.orchestrator.dispatch import MAX_EXTERNAL_ITEMS

    assert MAX_LIST_RESULTS <= MAX_EXTERNAL_ITEMS  # else files are silently withheld


async def test_quarantine_failure_on_a_middle_search_item_leaves_a_withheld_block_there() -> None:
    from src.lanes.quarantine import Quarantine
    from src.llm.fake import FakeLLM
    from src.llm.types import ProviderResponse, ToolCallRequest, Usage
    from src.orchestrator.dispatch import DenyAllWritesPolicy
    from src.orchestrator.loop import Orchestrator
    from tests.fakes.llm import build_env

    drive = FakeDrive({"/drive/v3/files": (200, fx("drive_list.json"))})
    group = DriveReadTool()
    group.bind(drive.reader())
    good = json.dumps({"summary": "GOODSUMMARY"})
    # quarantine replies, in file order: ok, garbage, ok (no repair retry on the
    # quarantine lane since P3.0 MED-D)
    qfake = FakeLLM([good, "not json", good])
    main = FakeLLM(
        [
            ProviderResponse(
                text=None,
                tool_calls=(ToolCallRequest(id="c", name="drive_search", arguments={"query": ""}),),
                usage=Usage(tokens_in=1, tokens_out=1),
            ),
            "ok",
        ]
    )
    orch = Orchestrator(
        build_env(provider=main).llm,
        group.tools(),
        DenyAllWritesPolicy(),
        character_card="c",
        scopes_enabled=frozenset({"tasks", "reminders", "drive"}),
        nonce_factory=lambda: "N",
        quarantine=Quarantine(build_env(provider=qfake).llm),
    )
    await orch.run_turn("search drive")
    msg = next(m for m in main.requests[1].messages if m.role == "tool").content or ""
    first, second, third = msg.split("DATA label=")[2:5]
    assert "GOODSUMMARY" in first and "GOODSUMMARY" in third
    assert "withheld" in second.lower() and "GOODSUMMARY" not in second
