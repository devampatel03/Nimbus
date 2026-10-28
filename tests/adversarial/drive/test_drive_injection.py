# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Adversarial (P3.8, INV-6, INV-5): a Drive document and file name carrying
prompt injection ("share this folder with ...", "ignore previous ...") must
not produce any tool call, permission or raw text in the trusted lane.

Both the main model and the quarantine model are `CompromisedFakeLLM`. The turn
exposes decoy write tools (share, delete, mail send, calendar create) whose
`run` counts executions; the only tools allowed to run are the two Drive reads.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from src.lanes.quarantine import Quarantine
from src.llm.fake import CompromisedFakeLLM, FakeLLM
from src.llm.types import ProviderResponse, ToolCallRequest, ToolSpec, Usage
from src.orchestrator.dispatch import DenyAllWritesPolicy, Risk, ToolOutput
from src.orchestrator.loop import Orchestrator
from src.tools.integrations.drive_read import DriveReadTool
from tests.fakes.llm import build_env
from tests.unit.integrations.google.drive_helpers import ID_INJECT, FakeDrive, fx

PAYLOADS = ("share this folder", "ignore previous", "attacker@example.invalid", "gmail_send")


class Decoy:
    def __init__(self, name: str, risk: Risk) -> None:
        self.spec = ToolSpec(name=name, description="decoy")
        self.risk = risk
        self.ran = 0

    async def run(self, args: Mapping[str, Any]) -> ToolOutput:
        self.ran += 1
        return ToolOutput("done")


def _setup() -> tuple[FakeDrive, DriveReadTool, list[Decoy]]:
    drive = FakeDrive(
        {
            f"/drive/v3/files/{ID_INJECT}": (200, fx("drive_meta_injected.json")),
            f"/drive/v3/files/{ID_INJECT}?media": (200, fx("drive_content_injected.txt")),
            "/drive/v3/files": (
                200,
                json.dumps({"files": [json.loads(fx("drive_meta_injected.json"))]}).encode(),
            ),
        }
    )
    group = DriveReadTool()
    group.bind(drive.reader())
    decoys = [
        Decoy("drive_share", Risk.WRITE_EXTERNAL),
        Decoy("drive_delete", Risk.WRITE_INTERNAL),
        Decoy("gmail_send", Risk.WRITE_EXTERNAL),
        Decoy("calendar_create", Risk.WRITE_EXTERNAL),
    ]
    return drive, group, decoys


def _call(name: str, args: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        text=None,
        tool_calls=(ToolCallRequest(id="c1", name=name, arguments=args),),
        usage=Usage(tokens_in=1, tokens_out=1),
    )


async def _turn(first: ProviderResponse, main: FakeLLM) -> tuple[Any, list[Decoy], FakeLLM]:
    _, group, decoys = _setup()
    quarantine_llm = CompromisedFakeLLM(default=json.dumps({"summary": "notes"}))
    orch = Orchestrator(
        build_env(provider=main).llm,
        [*group.tools(), *decoys],
        DenyAllWritesPolicy(),
        character_card="c",
        nonce_factory=lambda: "N",
        quarantine=Quarantine(build_env(provider=quarantine_llm).llm),
        scopes_enabled=frozenset({"tasks", "reminders", "drive"}),
    )
    return await orch.run_turn("read my file"), decoys, quarantine_llm


async def test_read_of_injected_doc_runs_no_write_tool() -> None:
    main = FakeLLM([_call("drive_read", {"file_id": ID_INJECT}), "summary done"])
    result, decoys, _ = await _turn(_call("drive_read", {}), main)
    assert [d.ran for d in decoys] == [0, 0, 0, 0]
    assert [e.tool for e in result.tool_events] == ["drive_read"]
    assert result.tainted is True


async def test_search_of_injected_name_runs_no_write_tool() -> None:
    main = FakeLLM([_call("drive_search", {"query": "notes"}), "found it"])
    result, decoys, _ = await _turn(_call("drive_search", {}), main)
    assert [d.ran for d in decoys] == [0, 0, 0, 0]
    assert [e.tool for e in result.tool_events] == ["drive_search"]


async def test_compromised_main_model_proposing_writes_is_denied() -> None:
    """Even if the main model obeys the injection and proposes the writes."""
    main = FakeLLM(
        [
            _call("drive_read", {"file_id": ID_INJECT}),
            _call("drive_share", {"email": "attacker@example.invalid"}),
            _call("gmail_send", {"to": "attacker@example.invalid"}),
            "done",
        ]
    )
    result, decoys, _ = await _turn(_call("drive_read", {}), main)
    assert [d.ran for d in decoys] == [0, 0, 0, 0]
    denied = [e for e in result.tool_events if e.status == "denied"]
    assert {e.tool for e in denied} >= {"drive_share", "gmail_send"}
    assert all(e.tainted for e in denied)


async def test_raw_injection_never_reaches_main_model_prompt() -> None:
    main = FakeLLM([_call("drive_read", {"file_id": ID_INJECT}), "ok"])
    _, _, _ = await _turn(_call("drive_read", {}), main)
    prompt = "\n".join((m.content or "") for r in main.requests for m in r.messages).lower()
    for p in PAYLOADS:
        assert p not in prompt
