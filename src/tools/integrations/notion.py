# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Notion as three `LoopTool`s (P3.7; ADR-0005, ADR-0009, ADR-0010, ADR-0021).

| tool           | risk           | policy                                   |
|----------------|----------------|------------------------------------------|
| notion_search  | READ           | runs (scope `notion` must be enabled)    |
| notion_read    | READ           | runs                                     |
| notion_append  | WRITE_EXTERNAL | approval card + TOTP step-up             |

Append only: there is no update, delete, archive, share, comment or permission
tool, and the client has no such method (`src.net` also refuses those calls).

Untrusted content (INV-6): page titles and bodies are authored by anyone with
access to the shared page. They leave a tool ONLY in `ToolOutput.external` as
`UntrustedText` (one item per page; the loop quarantines each). `ToolOutput.text`
carries counts, opaque handles and code-generated statuses, never Notion text and
never a Notion id. Handles look like `n<nonce>_<list>_<n>`; the nonce is random
per process, so a handle from before a restart fails closed. `resolve_args`
(dispatcher hook) maps a handle to the real page id and validates the text to
append BEFORE the policy and approval see the call, so the stored approval
payload carries the real id and the exact text that will be written. The card's
target line (`describe_args`) is the page title, labelled UNTRUSTED.

Quarantine budget (P3.0 MED-D): `notion_search` costs up to 5 quarantine calls
(one per page, `MAX_SEARCH_RESULTS` = loop `MAX_EXTERNAL_ITEMS`), `notion_read`
1, `notion_append` 0. With the loop cap of 8 tool calls per turn the worst case
is 8 x 5 = 40 quarantine calls for one turn of searches.

`ToolRegistry` builds groups with no arguments; until `bind()` hands over the
client every tool fails closed. Scope `notion` is not in `LOCAL_SCOPES`, so the
policy denies these tools (`deny.scope_disabled`) until the app enables it. Live
use needs H5 (`NOTION_TOKEN`, pages shared with the integration); the group is
not wired in `src/app/runtime.py` (HUMAN_TODO H26).
"""

from __future__ import annotations

import json
import re
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from src.core.turn_context import current_event_id
from src.integrations.notion.api import (
    MAX_SEARCH_RESULTS,
    NotionClient,
    NotionError,
    PageView,
    append_key,
    normalise_page_id,
    validate_append_text,
)
from src.lanes.data import SourceKind, UntrustedText
from src.orchestrator.dispatch import LoopTool, Risk, ToolError, ToolOutput
from src.tools.base import FunctionTool, ToolGroup, object_schema, validate_args

# Below the quarantine input cap (12_000), so the cut happens here, visibly.
MAX_EXTERNAL_CHARS = 8000
HANDLE_RE = re.compile(r"n[0-9a-f]{6}_\d{1,9}_\d{1,2}")
MAX_HANDLES = 256
TARGET_KEY = "_target"  # code-written card line; model-supplied values are dropped


def _guard(exc: NotionError) -> ToolError:
    return ToolError(str(exc))


class HandleBook:
    """Opaque, process-local stand-ins for page ids. Never persisted: the
    approval payload stores the real id, so only an unapproved handle goes stale
    on restart (and fails closed because the nonce changes)."""

    def __init__(self) -> None:
        self._nonce = secrets.token_hex(3)
        self._ids: OrderedDict[str, str] = OrderedDict()
        self._lists = 0

    def issue(self, page_ids: list[str]) -> list[str]:
        self._lists += 1
        handles = [f"n{self._nonce}_{self._lists}_{i}" for i, _ in enumerate(page_ids, 1)]
        self._ids.update(zip(handles, page_ids, strict=True))
        while len(self._ids) > MAX_HANDLES:
            self._ids.popitem(last=False)
        return handles

    def resolve(self, handle: object) -> str:
        if not isinstance(handle, str) or HANDLE_RE.fullmatch(handle) is None:
            raise ToolError("page must be a handle from notion_search.")
        real = self._ids.get(handle)
        if real is None:
            raise ToolError("Unknown page handle; run notion_search again.")
        return real


class _NotionTool(FunctionTool):
    """`FunctionTool` plus the dispatcher hooks (see `Dispatcher`)."""

    def __init__(
        self,
        *a: Any,
        resolve: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        describe: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
    ) -> None:
        super().__init__(*a)
        self.resolve_args = self._resolve_async(resolve)
        if describe is not None:
            self.describe_args = describe

    @staticmethod
    def _resolve_async(
        fn: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]:
        async def go(args: Mapping[str, Any]) -> Mapping[str, Any]:
            return fn(args)

        return go

    async def run(self, args: Mapping[str, Any]) -> ToolOutput:
        clean = {k: v for k, v in args.items() if k != TARGET_KEY}
        validate_args(self.spec.parameters, clean)
        return await self._handler(clean)


class NotionTool(ToolGroup):
    name = "notion"

    def __init__(self) -> None:
        self._client: NotionClient | None = None
        self._handles = HandleBook()

    def bind(self, client: NotionClient) -> None:
        self._client = client

    def _ready(self) -> NotionClient:
        if self._client is None:
            raise ToolError("notion is unavailable")
        return self._client

    def _resolve(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        """Model-path argument preparation: drop any model-supplied `_target`,
        turn a page handle into the real id and validate the append text."""
        out = {k: v for k, v in args.items() if k != TARGET_KEY}
        if "page" in out:
            out["page"] = self._handles.resolve(out["page"])
        if "text" in out:
            try:
                out["text"] = validate_append_text(out["text"])
            except NotionError as exc:
                raise _guard(exc) from None
        return out

    async def _describe(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        """Before an approval card: attach the target's title, labelled UNTRUSTED."""
        label = await self._ready().target_label(str(args.get("page", "")))
        return {**args, TARGET_KEY: label}

    async def _search(self, args: Mapping[str, Any]) -> ToolOutput:
        client = self._ready()
        try:
            pages = await client.search(args["query"])
        except NotionError as exc:
            raise _guard(exc) from None
        handles = self._handles.issue([p.page_id for p in pages])
        # One item per page, in page order: each is quarantined on its own, so no
        # title can forge another page's entry. Cost: up to 5 quarantine calls.
        external = tuple(_page_item(p) for p in pages[:MAX_SEARCH_RESULTS])
        body = {"count": len(pages), "pages": handles, "external_items_follow_page_order": True}
        return ToolOutput(json.dumps(body), external=external)

    async def _read(self, args: Mapping[str, Any]) -> ToolOutput:
        client = self._ready()
        try:
            pid = normalise_page_id(args["page"])
            view = await client.page(pid)
            body, more = await client.read_text(pid)
        except NotionError as exc:
            raise _guard(exc) from None
        whole = f"title: {view.title}\n{body}"
        clipped = whole[:MAX_EXTERNAL_CHARS]  # the WHOLE external string is capped
        text = UntrustedText(clipped, source=SourceKind.DOCUMENT)
        out = {"chars": len(clipped), "truncated": more or len(whole) > len(clipped)}
        return ToolOutput(json.dumps(out), external=(text,))

    async def _append(self, args: Mapping[str, Any]) -> ToolOutput:
        client = self._ready()
        try:
            pid = normalise_page_id(args["page"])
            text = validate_append_text(args["text"])
            source = current_event_id()
            if source is None:
                # No stored turn event: a deterministic key would collapse two
                # legitimate identical appends (or be unreplayable). Refuse.
                raise ToolError("notion_append can only run for a stored user turn.")
            key = append_key(source, pid, text)
            done = await client.append(pid, text, key)
        except NotionError as exc:
            raise _guard(exc) from None
        status = "appended" if done else "already_appended"
        return ToolOutput(json.dumps({"status": status}), untrusted=False, external=())

    def tools(self) -> list[LoopTool]:
        """Offered to the model only while bound (and, via the policy table, only
        when scope `notion` is enabled)."""
        return self.gate(self._all_tools(), lambda: self._client is not None)

    def _all_tools(self) -> list[LoopTool]:
        page = {"page": "string"}
        return [
            _NotionTool(
                "notion_search",
                "Search the Notion pages shared with the assistant; empty query lists recent "
                "pages. Read only. Returns page handles; titles arrive as untrusted data.",
                Risk.READ,
                object_schema({"query": "string"}),
                self._search,
                resolve=self._resolve,
            ),
            _NotionTool(
                "notion_read",
                "Read the text of one Notion page by its handle from notion_search. "
                "Read only. Content arrives as untrusted data.",
                Risk.READ,
                object_schema(page),
                self._read,
                resolve=self._resolve,
            ),
            _NotionTool(
                "notion_append",
                "Append plain text (max 6000 chars) to the end of a Notion page (handle from "
                "notion_search); pages over ~1000 blocks are refused. A small gray [nimbus:id] "
                "tag is added. Cannot edit or delete. Needs approval and authenticator code.",
                Risk.WRITE_EXTERNAL,
                object_schema({**page, "text": "string"}),
                self._append,
                resolve=self._resolve,
                describe=self._describe,
            ),
        ]


def _page_item(p: PageView) -> UntrustedText:
    edited = f" (edited {p.edited})" if p.edited else ""
    return UntrustedText(
        f"title: {p.title}{edited}"[:MAX_EXTERNAL_CHARS], source=SourceKind.DOCUMENT
    )
