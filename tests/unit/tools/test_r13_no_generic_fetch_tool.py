# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.5 / R13: no generic fetch/HTTP tool exists. Every tool the model can call
either is local or targets one fixed Service; none takes a URL or host."""

from __future__ import annotations

import re

import pytest

from src.tools.registry import ToolRegistry

_URL_PARAM = re.compile(
    r"(^|_)(url|uri|href|link|host|hostname|endpoint|domain|address|base_url|proxy)(s)?($|_)",
    re.IGNORECASE,
)
_FETCH_NAME = re.compile(
    r"(fetch|http|request|curl|wget|browse|download|scrape|crawl|web|url|open_link|proxy)",
    re.IGNORECASE,
)
# Reviewed exceptions: tool names that legitimately contain a matched word.
_NAME_ALLOW: frozenset[str] = frozenset()


@pytest.fixture(scope="module")
def tools() -> list:  # type: ignore[type-arg]
    return ToolRegistry().loop_tools()


def test_registry_is_not_empty(tools: list) -> None:  # type: ignore[type-arg]
    assert len(tools) >= 5


def test_no_tool_named_like_a_fetcher(tools: list) -> None:  # type: ignore[type-arg]
    bad = [
        t.spec.name
        for t in tools
        if t.spec.name not in _NAME_ALLOW and _FETCH_NAME.search(t.spec.name)
    ]
    assert bad == []


def test_no_tool_accepts_a_url_or_host_argument(tools: list) -> None:  # type: ignore[type-arg]
    bad = [
        (t.spec.name, k)
        for t in tools
        for k in t.spec.parameters.get("properties", {})
        if _URL_PARAM.search(k)
    ]
    assert bad == []


def test_guard_regexes_can_fail() -> None:
    """Prove the tripwires fire on the shapes they exist for."""
    assert _FETCH_NAME.search("http_get") and _FETCH_NAME.search("fetch_page")
    assert _URL_PARAM.search("url") and _URL_PARAM.search("target_host")
    assert not _URL_PARAM.search("title") and not _URL_PARAM.search("calendar_id")
