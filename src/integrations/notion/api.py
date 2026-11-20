# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Notion REST client over the egress client (P3.7; ADR-0004, ADR-0021, R13).

Exactly four calls exist (and `src.net` refuses everything else on
`api.notion.com`): search pages, read one page's properties, read a page's
child blocks, append child blocks. There is no update, delete, archive, share
or comment method. The integration token only reaches pages the user shared
with the integration in Notion; "not shared" is reported as "not found".

Everything Notion returns that anyone can author (titles, block text) is
untrusted: it is sanitised (HTML stripped, hidden and bidi characters dropped,
sizes capped) and handed back only as plain `str` for the tool layer to wrap in
`UntrustedText`. Page ids are validated UUIDs and are never shown to the model
(the tool issues opaque handles). Error messages are fixed text and never echo
response bodies. The token is a `SecretStr`, only unwrapped into the request
header, never logged.

Append idempotency: Notion has no idempotency key, so the appended text ends
with a gray marker `[nimbus:<key>]`; `append` first scans the page for it and
returns False (already done) instead of writing a second time. A page too long
to scan fails closed. Rate limits (429) are retried with bounded backoff;
other failures are not retried (a transient failure is reported to the user).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import SecretStr

from src.net import EgressClient, EgressError, Service

NOTION_VERSION: Final = "2022-06-28"
MAX_RESPONSE_BYTES: Final = 512 * 1024
# Equals the loop's MAX_EXTERNAL_ITEMS: one quarantine call per page.
MAX_SEARCH_RESULTS: Final = 5
MAX_QUERY_CHARS: Final = 200
MAX_TITLE_CHARS: Final = 200
MAX_READ_CHARS: Final = 7500  # below the tool's external cap
MAX_READ_REQUESTS: Final = 3  # 3 x 100 top-level blocks
MAX_SCAN_REQUESTS: Final = 10  # idempotency scan: 10 x 100 blocks
MAX_APPEND_CHARS: Final = 6000
CHUNK_CHARS: Final = 1900  # Notion limit is 2000 per rich-text object
MAX_ATTEMPTS: Final = 3
MAX_BACKOFF_S: Final = 8.0

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)
_CURSOR_RE = re.compile(r"[0-9a-fA-F-]{1,64}")
_TIME_RE = re.compile(r"[0-9T:.\-Z+]{10,40}")
_TAG_RE = re.compile(r"<[^>]{0,2000}>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TEXT_BLOCKS: Final = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "quote",
        "callout",
        "toggle",
        "code",
    }
)


class NotionError(Exception):
    """Sanitised failure; the message is fixed text, safe to show the model."""

    def __init__(self, message: str, *, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


# -- sanitising --------------------------------------------------------------


def clean_external(raw: object, limit: int) -> str:
    """Strip HTML and hidden text from third-party text and cap its size."""
    if not isinstance(raw, str):
        return ""
    text = _COMMENT_RE.sub(" ", raw[: limit * 8])
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", html.unescape(text))
    kept: list[str] = []
    for c in text:
        cat = unicodedata.category(c)
        if c == "\n":
            kept.append(c)
        elif c in "\r\t":
            kept.append(" ")
        elif cat[0] == "C" or cat in ("Zl", "Zp"):
            continue  # control, format (zero-width, bidi), private use
        else:
            kept.append(c)
    out = re.sub(r" {2,}", " ", "".join(kept))
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out[:limit]


def one_line(raw: object, limit: int) -> str:
    return " ".join(clean_external(raw, limit * 2).split())[:limit]


def normalise_page_id(raw: object) -> str:
    """Dashed lowercase UUID, or `NotionError`. Anything else never reaches a path."""
    if not isinstance(raw, str) or _UUID_RE.fullmatch(raw) is None:
        raise NotionError("That page id is not valid.", code="bad_id")
    return str(uuid.UUID(raw))


def clean_query(raw: object) -> str:
    if not isinstance(raw, str):
        raise NotionError("query must be text.")
    return "".join(c for c in raw if c.isprintable())[:MAX_QUERY_CHARS].strip()


def validate_append_text(raw: object) -> str:
    """The exact text to append, or `NotionError`. Hidden characters are REJECTED
    (not dropped) so the text the user approves is the text that is written."""
    if not isinstance(raw, str):
        raise NotionError("text must be text.")
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    for c in text:
        if c in "\n\t":
            continue
        cat = unicodedata.category(c)
        if cat[0] == "C" or cat in ("Zl", "Zp"):
            raise NotionError("text contains control or hidden characters.")
    text = text.strip()
    if not text:
        raise NotionError("text is required.")
    if len(text) > MAX_APPEND_CHARS:
        raise NotionError(f"text is too long (limit {MAX_APPEND_CHARS} characters).")
    return text


def append_key(source_event_id: uuid.UUID | None, page_id: str, text: str) -> str:
    """Deterministic idempotency key: the same turn appending the same text to
    the same page always gets the same key."""
    seed = f"nimbus-notion-v1|{source_event_id or 'none'}|{page_id}|{text}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def marker(key: str) -> str:
    return f"[nimbus:{key}]"


def chunk_text(text: str) -> list[str]:
    """Pack lines into chunks of at most `CHUNK_CHARS` (Notion's 2000-char limit
    per rich-text object); a longer line is cut."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > CHUNK_CHARS:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:CHUNK_CHARS])
            line = line[CHUNK_CHARS:]
        if cur and len(cur) + 1 + len(line) > CHUNK_CHARS:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def build_blocks(text: str, key: str) -> list[dict[str, Any]]:
    """Paragraph blocks of plain text only (no links, no mentions). The last one
    ends with the gray idempotency marker."""
    chunks = chunk_text(text)
    blocks: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        rich: list[dict[str, Any]] = [{"type": "text", "text": {"content": chunk}}]
        if i == len(chunks) - 1:
            rich.append(
                {
                    "type": "text",
                    "text": {"content": f" {marker(key)}"},
                    "annotations": {"color": "gray"},
                }
            )
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}})
    return blocks


# -- reading Notion's answer -------------------------------------------------


@dataclass(frozen=True)
class PageView:
    """`page_id` is a validated UUID; `title` is third-party text."""

    page_id: str
    title: str
    edited: str


def _rich_plain(rich: object) -> str:
    if not isinstance(rich, list):
        return ""
    parts = [r.get("plain_text") for r in rich[:100] if isinstance(r, dict)]
    return "".join(p for p in parts if isinstance(p, str))


def _title_of(page: Mapping[str, Any]) -> str:
    props = page.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return one_line(_rich_plain(prop.get("title")), MAX_TITLE_CHARS)
    return ""


def to_page(item: object) -> PageView | None:
    if not isinstance(item, dict) or item.get("object") != "page":
        return None
    if item.get("archived") is True or item.get("in_trash") is True:
        return None
    try:
        pid = normalise_page_id(item.get("id"))
    except NotionError:
        return None
    edited = item.get("last_edited_time")
    ts = edited if isinstance(edited, str) and _TIME_RE.fullmatch(edited) else ""
    return PageView(pid, _title_of(item), ts)


def block_texts(results: object) -> list[str]:
    """Plain text of the supported block types, sanitised, one entry per block."""
    out: list[str] = []
    if not isinstance(results, list):
        return out
    for blk in results:
        if not isinstance(blk, dict):
            continue
        kind = blk.get("type")
        if kind not in _TEXT_BLOCKS:
            continue
        body = blk.get(kind)
        if isinstance(body, dict):
            text = clean_external(_rich_plain(body.get("rich_text")), CHUNK_CHARS * 2)
            if text:
                out.append(text)
    return out


# -- the HTTP client ---------------------------------------------------------

_NOT_FOUND = "That Notion page was not found or is not shared with the integration."
_FIXED_ERRORS: Mapping[int, tuple[str, str]] = {
    400: ("Notion rejected the request.", "bad_request"),
    401: ("Notion access was refused; the integration token may need replacing.", "unauthorized"),
    403: (_NOT_FOUND, "not_found"),
    404: (_NOT_FOUND, "not_found"),
    409: ("Notion reported a conflict; try again later.", "conflict"),
    429: ("Notion is rate limiting requests; try again later.", "rate_limited"),
}

Sleeper = Callable[[float], Awaitable[None]]


def _backoff(retry_after: str | None, attempt: int) -> float:
    """Seconds to wait before retry `attempt`: Retry-After when sane, else
    exponential; always at most `MAX_BACKOFF_S`."""
    try:
        wait = float(retry_after) if retry_after is not None else 0.0
    except ValueError:
        wait = 0.0
    if wait <= 0.0:
        wait = 0.5 * 2**attempt
    return min(wait, MAX_BACKOFF_S)


class NotionClient:
    def __init__(
        self, egress: EgressClient, token: SecretStr, *, sleep: Sleeper = asyncio.sleep
    ) -> None:
        self._egress = egress
        self._token = token
        self._sleep = sleep

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        headers = {
            "authorization": f"Bearer {self._token.get_secret_value()}",
            "notion-version": NOTION_VERSION,
            "accept": "application/json",
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._egress.request(
                    Service.NOTION,
                    method,
                    path,
                    params=dict(params) if params else None,
                    headers=headers,
                    json=dict(body) if body is not None else None,
                    max_bytes=MAX_RESPONSE_BYTES,
                )
            except EgressError:
                raise NotionError("Notion could not be reached.", code="network") from None
            if resp.status_code == 429 and attempt < MAX_ATTEMPTS:
                # 429 means the request was NOT processed, so a retry is safe even for append.
                await self._sleep(_backoff(resp.headers.get("retry-after"), attempt))
                continue
            break
        if resp.is_success:
            try:
                return resp.json() if resp.content else {}
            except ValueError:
                raise NotionError("Notion sent an unreadable reply.") from None
        if resp.status_code >= 500:
            raise NotionError("Notion is having problems; try again later.", code="5xx")
        msg, code = _FIXED_ERRORS.get(resp.status_code, ("Notion request failed.", "error"))
        raise NotionError(msg, code=code)

    async def search(self, query: str) -> list[PageView]:
        data = await self._call(
            "POST",
            "/v1/search",
            body={
                "query": clean_query(query),
                "page_size": MAX_SEARCH_RESULTS,
                "filter": {"property": "object", "value": "page"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            },
        )
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        pages = (to_page(i) for i in items[:MAX_SEARCH_RESULTS])
        return [p for p in pages if p is not None]

    async def page(self, page_id: str) -> PageView:
        pid = normalise_page_id(page_id)
        view = to_page(await self._call("GET", f"/v1/pages/{pid}"))
        if view is None or view.page_id != pid:
            raise NotionError(_NOT_FOUND, code="not_found")
        return view

    async def _children(self, pid: str, cursor: str | None) -> dict[str, Any]:
        params = {"page_size": "100"}
        if cursor:
            params["start_cursor"] = cursor
        data = await self._call("GET", f"/v1/blocks/{pid}/children", params=params)
        if not isinstance(data, dict):
            raise NotionError("Notion sent an unreadable reply.")
        return data

    @staticmethod
    def _next_cursor(data: Mapping[str, Any]) -> str | None:
        nxt = data.get("next_cursor")
        if data.get("has_more") is True and isinstance(nxt, str) and _CURSOR_RE.fullmatch(nxt):
            return nxt
        return None

    async def read_text(self, page_id: str) -> tuple[str, bool]:
        """(text, truncated): top-level text blocks, at most `MAX_READ_REQUESTS`
        requests and `MAX_READ_CHARS` characters. Nested blocks are not read."""
        pid = normalise_page_id(page_id)
        lines: list[str] = []
        total = 0
        cursor: str | None = None
        for _ in range(MAX_READ_REQUESTS):
            data = await self._children(pid, cursor)
            for line in block_texts(data.get("results")):
                total += len(line) + 1
                lines.append(line)
                if total > MAX_READ_CHARS:
                    return "\n".join(lines)[:MAX_READ_CHARS], True
            cursor = self._next_cursor(data)
            if cursor is None:
                return "\n".join(lines), data.get("has_more") is True
        return "\n".join(lines), True

    async def _has_marker(self, pid: str, key: str) -> bool:
        needle = marker(key)
        cursor: str | None = None
        for _ in range(MAX_SCAN_REQUESTS):
            data = await self._children(pid, cursor)
            results = data.get("results")
            for blk in results if isinstance(results, list) else []:
                body = blk.get(blk.get("type")) if isinstance(blk, dict) else None
                if isinstance(body, dict) and needle in _rich_plain(body.get("rich_text")):
                    return True
            cursor = self._next_cursor(data)
            if cursor is None:
                if data.get("has_more") is True:
                    break
                return False
        raise NotionError(
            "That page is too long to check for a repeated append; nothing was written.",
            code="too_long",
        )

    async def append(self, page_id: str, text: str, key: str) -> bool:
        """True when appended now, False when this exact append is already on the page."""
        pid = normalise_page_id(page_id)
        clean = validate_append_text(text)
        await self.page(pid)  # not shared / archived -> fixed error, nothing written
        if await self._has_marker(pid, key):
            return False
        await self._call(
            "PATCH", f"/v1/blocks/{pid}/children", body={"children": build_blocks(clean, key)}
        )
        return True

    async def target_label(self, page_id: str) -> str:
        """One line for an approval card: the page title, labelled UNTRUSTED.
        Never raises; a failed fetch is 'target unverified'."""
        try:
            view = await self.page(page_id)
        except NotionError:
            return "target unverified (the page could not be fetched)"
        return (
            "UNTRUSTED page title from Notion, shown for identification only: "
            f"title={one_line(view.title, 80)!r}"
        )
