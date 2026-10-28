# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Google Drive, read only, as two `LoopTool`s (P3.8; ADR-0005, INV-6, R13).

* `drive_search`: list recent files, optionally matching a word.
* `drive_read`: text content of one file (plain text, markdown, csv, json,
  Google Docs/Sheets/Slides as text). Binary and oversized files are refused.

Both are READ risk, scope `drive` (OAuth `drive.readonly`). There is no
create/update/delete/share tool and no such client method. File names, owners
and contents are attacker-controlled: they leave this module ONLY as one
`UntrustedText` in `ToolOutput.external` (quarantined by the loop; one item per
call so the per-turn quarantine budget is not worsened), clipped to
`MAX_EXTERNAL_CHARS`. `ToolOutput.text` carries only counts, Drive ids and
validated metadata (size, MIME from an allow-list, timestamps), never a name.

The group starts unbound and fails closed until `bind()` (live wiring needs H4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from src.integrations.google.drive_api import (
    FILE_ID_RE,
    READABLE_TYPES,
    DriveError,
    DriveReader,
)
from src.lanes.data import SourceKind, UntrustedText
from src.orchestrator.dispatch import LoopTool, Risk, ToolError, ToolOutput
from src.tools.base import FunctionTool, ToolGroup, object_schema

# Below the quarantine input cap (12_000), so the cut happens here, visibly.
MAX_EXTERNAL_CHARS = 8000

_MESSAGES = {
    "reauth_required": "google drive needs re-authorisation by the operator",
    "not_found": "file not found or not accessible",
    "unsupported_type": "this file type cannot be read as text",
    "too_large": "file is too large to read",
    "bad_id": "invalid file id",
}


def _err(exc: DriveError) -> ToolError:
    return ToolError(_MESSAGES.get(exc.code, "google drive is unavailable"))


class DriveReadTool(ToolGroup):
    name = "drive"

    def __init__(self) -> None:
        self._reader: DriveReader | None = None

    def bind(self, reader: DriveReader) -> None:
        self._reader = reader

    def _ready(self) -> DriveReader:
        if self._reader is None:
            raise ToolError("google drive is unavailable")
        return self._reader

    async def _search(self, args: Mapping[str, Any]) -> ToolOutput:
        reader = self._ready()
        try:
            files = await reader.search(args["query"])
        except DriveError as exc:
            raise _err(exc) from None
        meta = [
            {
                "n": i + 1,
                "file_id": f.file_id,
                "mime_type": f.mime_type if f.mime_type in READABLE_TYPES else "other",
                "readable": f.mime_type in READABLE_TYPES,
                "size_bytes": f.size,
                "modified": f.modified,
            }
            for i, f in enumerate(files)
        ]
        # One item per file, in the same order as `files` (n = position): each is
        # quarantined on its own, so no name can forge another file's entry. Cost:
        # up to MAX_LIST_RESULTS (5) quarantine calls for one search.
        external = tuple(
            UntrustedText(
                f"{f.name} (owner: {', '.join(f.owners) or 'unknown'})"[:MAX_EXTERNAL_CHARS],
                source=SourceKind.DOCUMENT,
            )
            for f in files
        )
        out = {"count": len(meta), "files": meta, "external_items_follow_file_order": True}
        return ToolOutput(json.dumps(out), external=external)

    async def _read(self, args: Mapping[str, Any]) -> ToolOutput:
        reader = self._ready()
        file_id = args["file_id"]
        if not FILE_ID_RE.fullmatch(file_id):
            raise ToolError(_MESSAGES["bad_id"])
        try:
            f = await reader.metadata(file_id)
            raw = await reader.read_text(f)
        except DriveError as exc:
            raise _err(exc) from None
        decoded = raw.decode("utf-8", errors="replace")
        whole = f"title: {f.name}\n{decoded}"
        clipped = whole[:MAX_EXTERNAL_CHARS]  # the WHOLE external string is capped
        text = UntrustedText(clipped, source=SourceKind.DOCUMENT)
        out = {
            "file_id": f.file_id,
            "mime_type": f.mime_type,
            "size_bytes": f.size,
            "modified": f.modified,
            "chars": len(clipped),
            "truncated": len(whole) > len(clipped),
        }
        return ToolOutput(json.dumps(out), external=(text,))

    def tools(self) -> list[LoopTool]:
        """Offered to the model only while bound (and, via the policy table, only
        when scope `drive` is enabled)."""
        return self.gate(self._all_tools(), lambda: self._reader is not None)

    def _all_tools(self) -> list[LoopTool]:
        return [
            FunctionTool(
                "drive_search",
                "Search Google Drive file names and text; empty query lists recent files. "
                "Read only. Returns file ids; names arrive as untrusted data.",
                Risk.READ,
                object_schema({"query": "string"}),
                self._search,
            ),
            FunctionTool(
                "drive_read",
                "Read the text of one Drive file by id (text, markdown, csv, json, Google "
                "Docs/Sheets/Slides). Binary and large files are refused. Read only.",
                Risk.READ,
                object_schema({"file_id": "string"}),
                self._read,
            ),
        ]
