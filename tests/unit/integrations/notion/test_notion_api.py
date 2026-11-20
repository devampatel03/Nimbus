# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P3.7: Notion client against recorded fixtures (no live calls)."""

from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest

from src.integrations.notion import api
from src.integrations.notion.api import (
    NotionError,
    append_key,
    build_blocks,
    chunk_text,
    clean_external,
    normalise_page_id,
    validate_append_text,
)
from src.integrations.notion.settings import NotionSettings
from tests.unit.integrations.notion.notion_helpers import P1, P2, TOKEN, FakeNotion, fx, ok

CH1 = ("GET", f"/v1/blocks/{P1}/children")
PATCH1 = ("PATCH", f"/v1/blocks/{P1}/children")
PAGE1 = ("GET", f"/v1/pages/{P1}")
KEY = "k" * 20


async def test_search_filters_to_live_pages_and_sends_headers() -> None:
    n = FakeNotion({("POST", "/v1/search"): [ok(fx("notion_search.json"))]})
    pages = await n.client().search("trip")
    assert [p.page_id for p in pages] == [P1, P2]  # database and archived page dropped
    req = n.requests[0]
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["notion-version"] == api.NOTION_VERSION
    body = json.loads(req.content)
    assert body["page_size"] == 5 and body["filter"] == {"property": "object", "value": "page"}


async def test_search_query_is_cleaned_and_capped() -> None:
    n = FakeNotion({("POST", "/v1/search"): [ok({"results": []})]})
    await n.client().search("a\x00b\u202e" + "x" * 500)
    sent = json.loads(n.requests[0].content)["query"]
    assert len(sent) == api.MAX_QUERY_CHARS and "\x00" not in sent


@pytest.mark.parametrize("bad", [None, [], {"results": "no"}, "text", 5])
async def test_search_malformed_replies_yield_nothing(bad: object) -> None:
    n = FakeNotion({("POST", "/v1/search"): [ok(bad)]})
    assert await n.client().search("x") == []


async def test_hostile_search_items_are_skipped_not_trusted() -> None:
    hostile = {
        "results": [
            {"object": "page", "id": "../../users/me", "properties": {}},
            {"object": "page", "id": P1 + "/children", "properties": {}},
            {"object": "page", "id": 5},
            "str",
            {
                "object": "page",
                "id": P2,
                "last_edited_time": "x\nIgnore previous",
                "properties": {},
            },
        ]
    }
    n = FakeNotion({("POST", "/v1/search"): [ok(hostile)]})
    pages = await n.client().search("")
    assert [p.page_id for p in pages] == [P2] and pages[0].edited == ""


async def test_page_title_is_one_clean_line() -> None:
    page = fx("notion_page.json")
    page["properties"]["Name"]["title"][0]["plain_text"] = "A\u200b\nB\u202e<b>C</b>" + "z" * 400
    n = FakeNotion({PAGE1: [ok(page)]})
    v = await n.client().page(P1)
    assert "\n" not in v.title and "\u200b" not in v.title and "<" not in v.title
    assert len(v.title) <= api.MAX_TITLE_CHARS


async def test_page_with_a_different_id_is_not_found() -> None:
    n = FakeNotion({PAGE1: [ok(fx("notion_page_injected.json"))]})  # P2 returned for P1
    with pytest.raises(NotionError) as e:
        await n.client().page(P1)
    assert e.value.code == "not_found"


async def test_not_shared_is_a_graceful_fixed_message_without_body_echo() -> None:
    n = FakeNotion({PAGE1: [ok(fx("notion_error_404.json"), 404)]})
    with pytest.raises(NotionError) as e:
        await n.client().page(P1)
    assert "not shared" in str(e.value) and "SECRET" not in str(e.value)
    assert e.value.code == "not_found"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "bad_request"),
        (401, "unauthorized"),
        (403, "not_found"),
        (409, "conflict"),
        (418, "error"),
    ],
)
async def test_error_statuses_map_to_fixed_codes(status: int, code: str) -> None:
    n = FakeNotion({PAGE1: [ok({"message": TOKEN}, status)]})
    with pytest.raises(NotionError) as e:
        await n.client().page(P1)
    assert e.value.code == code and TOKEN not in str(e.value)


async def test_5xx_network_and_bad_json_fail_closed() -> None:
    n = FakeNotion({PAGE1: [ok({}, 503)]})
    with pytest.raises(NotionError, match="problems"):
        await n.client().page(P1)
    n = FakeNotion({PAGE1: [httpx.Response(200, content=b"<html>")]})
    with pytest.raises(NotionError, match="unreadable"):
        await n.client().page(P1)

    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    n = FakeNotion({PAGE1: [boom]})
    with pytest.raises(NotionError, match="could not be reached"):
        await n.client().page(P1)


async def test_rate_limit_retries_with_bounded_backoff_then_succeeds() -> None:
    limited = httpx.Response(429, headers={"retry-after": "2"}, json={})
    n = FakeNotion({PAGE1: [limited, limited, ok(fx("notion_page.json"))]})
    assert (await n.client().page(P1)).page_id == P1
    assert n.sleeps == [2.0, 2.0] and len(n.requests) == 3


async def test_rate_limit_gives_up_after_max_attempts_and_caps_sleep() -> None:
    limited = httpx.Response(429, headers={"retry-after": "9999"}, json={})
    n = FakeNotion({PAGE1: [limited]})
    with pytest.raises(NotionError) as e:
        await n.client().page(P1)
    assert e.value.code == "rate_limited" and len(n.requests) == api.MAX_ATTEMPTS
    assert len(n.sleeps) == api.MAX_ATTEMPTS - 1
    assert all(s <= api.MAX_BACKOFF_S for s in n.sleeps)


@pytest.mark.parametrize("hdr", [None, "abc", "-1", "0"])
async def test_backoff_without_usable_retry_after_is_exponential(hdr: str | None) -> None:
    headers = {} if hdr is None else {"retry-after": hdr}
    limited = httpx.Response(429, headers=headers, json={})
    n = FakeNotion({PAGE1: [limited, ok(fx("notion_page.json"))]})
    await n.client().page(P1)
    assert 0 < n.sleeps[0] <= api.MAX_BACKOFF_S


async def test_read_text_extracts_supported_blocks_only() -> None:
    n = FakeNotion({CH1: [ok(fx("notion_children.json"))]})
    text, more = await n.client().read_text(P1)
    assert text == "Flights booked for Nov 2\nHotel: near the station" and more is False


async def test_read_text_strips_html_and_hidden_chars() -> None:
    n = FakeNotion({CH1: [ok(fx("notion_children_injected.json"))]})
    text, _ = await n.client().read_text(P1)
    assert "<script" not in text and "alert(1)" not in text and "<!--" not in text
    assert "\u200b" not in text and "\u202e" not in text


async def test_read_text_is_capped() -> None:
    block = fx("notion_children.json")["results"][1]
    block["paragraph"]["rich_text"][0]["plain_text"] = "y" * 3000
    n = FakeNotion({CH1: [ok({"results": [block] * 6, "has_more": False})]})
    text, more = await n.client().read_text(P1)
    assert len(text) <= api.MAX_READ_CHARS and more is True


async def test_read_text_paginates_with_validated_cursor_and_request_cap() -> None:
    first = {"results": [], "has_more": True, "next_cursor": P2}
    n = FakeNotion({CH1: [ok(first)]})
    _, more = await n.client().read_text(P1)
    assert more is True and len(n.requests) == api.MAX_READ_REQUESTS
    assert n.requests[1].url.params["start_cursor"] == P2
    bad = {"results": [], "has_more": True, "next_cursor": "../../x?y=1"}
    n = FakeNotion({CH1: [ok(bad)]})
    _, more = await n.client().read_text(P1)
    assert more is True and len(n.requests) == 1  # a hostile cursor is never sent


@pytest.mark.parametrize(
    "raw", [None, 5, "x", "../users", P1 + "/children", P1.replace("-", "") + "0"]
)
def test_normalise_page_id_rejects_garbage(raw: object) -> None:
    with pytest.raises(NotionError):
        normalise_page_id(raw)


def test_normalise_page_id_accepts_uuid_forms() -> None:
    assert normalise_page_id(P1.upper()) == P1
    assert normalise_page_id(P1.replace("-", "")) == P1


@pytest.mark.parametrize(
    "text",
    ["", "   ", "a\x00b", "a\u200bb", "a\u202eb", "a\u2028b", "x" * (api.MAX_APPEND_CHARS + 1), 5],
)
def test_append_text_rejects_hidden_empty_and_oversize(text: object) -> None:
    with pytest.raises(NotionError):
        validate_append_text(text)


def test_append_text_normalises_newlines_and_keeps_tabs() -> None:
    assert validate_append_text("a\r\nb\tc\r") == "a\nb\tc"


def test_chunking_respects_notion_limit_and_loses_nothing() -> None:
    text = "\n".join(["l" * 1500, "m" * 1500, "n" * 5000, "end"])
    chunks = chunk_text(text)
    assert all(len(c) <= api.CHUNK_CHARS for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    blocks = build_blocks(text, KEY)
    assert len(blocks) == len(chunks) <= 100
    assert f"[nimbus:{KEY}]" in json.dumps(blocks[-1])
    assert all("[nimbus:" not in json.dumps(b) for b in blocks[:-1])
    for b in blocks:  # plain text only: no links, no mentions
        assert set(b["paragraph"]) == {"rich_text"}
        for r in b["paragraph"]["rich_text"]:
            assert r["type"] == "text" and "link" not in r["text"]


def test_append_key_is_deterministic_and_input_sensitive() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert append_key(a, P1, "t") == append_key(a, P1, "t")
    keys = {append_key(a, P1, "t"), append_key(b, P1, "t"), append_key(a, P2, "t")}
    keys.add(append_key(a, P1, "u"))
    assert len(keys) == 4


async def test_append_writes_once_with_marker_after_checking_the_page() -> None:
    n = FakeNotion(
        {
            PAGE1: [ok(fx("notion_page.json"))],
            CH1: [ok(fx("notion_children.json"))],
            PATCH1: [ok({})],
        }
    )
    key = append_key(uuid.UUID(int=7), P1, "buy milk")
    assert await n.client().append(P1, "buy milk", key) is True
    assert n.methods() == ["GET", "GET", "PATCH"]
    sent = json.loads(n.requests[2].content)
    assert sent["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "buy milk"
    assert f"[nimbus:{key}]" in json.dumps(sent)


async def test_replay_does_not_duplicate() -> None:
    key = append_key(uuid.UUID(int=7), P1, "buy milk")
    done = fx("notion_children.json")
    done["results"].append(
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"plain_text": "buy milk"}, {"plain_text": f" [nimbus:{key}]"}]
            },
        }
    )
    n = FakeNotion({PAGE1: [ok(fx("notion_page.json"))], CH1: [ok(done)], PATCH1: [ok({})]})
    assert await n.client().append(P1, "buy milk", key) is False
    assert "PATCH" not in n.methods()


async def test_marker_scan_is_capped_and_fails_closed_on_long_pages() -> None:
    more = {"results": [], "has_more": True, "next_cursor": P2}
    n = FakeNotion({PAGE1: [ok(fx("notion_page.json"))], CH1: [ok(more)], PATCH1: [ok({})]})
    with pytest.raises(NotionError) as e:
        await n.client().append(P1, "x", KEY)
    assert e.value.code == "too_long" and "PATCH" not in n.methods()
    assert len(n.requests) == 1 + api.MAX_SCAN_REQUESTS


async def test_append_to_unshared_page_writes_nothing() -> None:
    n = FakeNotion({PAGE1: [ok(fx("notion_error_404.json"), 404)], PATCH1: [ok({})]})
    with pytest.raises(NotionError, match="not shared"):
        await n.client().append(P1, "x", KEY)
    assert n.methods() == ["GET"]


async def test_append_retries_429_and_still_writes_once() -> None:
    limited = httpx.Response(429, headers={"retry-after": "1"}, json={})
    n = FakeNotion(
        {
            PAGE1: [ok(fx("notion_page.json"))],
            CH1: [ok(fx("notion_children.json"))],
            PATCH1: [limited, ok({})],
        }
    )
    assert await n.client().append(P1, "x", KEY) is True
    assert n.methods().count("PATCH") == 2 and n.sleeps == [1.0]


async def test_target_label_is_labelled_untrusted_and_never_raises() -> None:
    n = FakeNotion({PAGE1: [ok(fx("notion_page.json"))]})
    label = await n.client().target_label(P1)
    assert label.startswith("UNTRUSTED") and "Trip plan" in label
    assert (await FakeNotion({}).client().target_label(P1)).startswith("target unverified")
    assert (await FakeNotion({}).client().target_label("nonsense")).startswith("target unverified")


def test_clean_external_strips_markup_and_hidden_text() -> None:
    out = clean_external("a<script>x()</script>b<!-- c -->d<i>e</i>\u200bf\u202eg", 100)
    assert "<" not in out and "x()" not in out and "c" not in out.replace("g", "")
    assert "\u200b" not in out and "\u202e" not in out


async def test_token_never_appears_in_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    n = FakeNotion({PAGE1: [ok({}, 401)]})
    client = n.client()
    with pytest.raises(NotionError) as e:
        await client.page(P1)
    assert TOKEN not in str(e.value) and TOKEN not in caplog.text
    assert TOKEN not in repr(client._token)


def test_settings_token_is_hidden_and_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert NotionSettings(_env_file=None).token is None
    monkeypatch.setenv("NOTION_TOKEN", TOKEN)
    s = NotionSettings(_env_file=None)
    assert s.token is not None and TOKEN not in repr(s) and TOKEN not in str(s)
    assert s.token.get_secret_value() == TOKEN
