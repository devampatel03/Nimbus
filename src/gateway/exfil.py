# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Outbound exfil filter (P4.3): deterministic code between the model reply
and the Telegram send. The model is assumed fully compromised by any text it
read, so nothing here trusts the reply.

Threat: a reply carrying a clickable link (or image) whose URL holds private
data (`https://evil/?d=<secret>`). Link previews are off on every send (no
server-side fetch), so the remaining channel is a link the user taps. Telegram
auto-links URLs and bare domains even in escaped HTML text, so we must
neutralise them in the text itself.

Pipeline (`filter_reply`):
1. Normalise: CRLF/C0 controls removed, NFKC (full-width letters and dots
   become ASCII), invisible format characters (zero-width, bidi, soft hyphen,
   BOM, tags) dropped, ideographic full stops between word characters and
   ASCII folded to `.`. The normalised text is what is sent.
2. Neutralise (one left-to-right pass, first alternative wins):
   markdown / HTML images -> `[image removed]` (always, even allowlisted);
   reference definitions -> `[link removed]`; `[label](url)` -> label + url
   if allowed else label + `[link removed]`; scheme URLs (`x://`, `javascript:`,
   `data:`, `tg:`, `mailto:` ...), IPv4 literals and bare domains with a port,
   path or query -> `[link removed]` unless allowed; a bare host (or IP) with
   nothing after it is defanged (`evil[.]com`), which Telegram does not link.
3. Post-check: an independent scan of the result for any scheme URL, IP or
   domain that the allowlist does not permit. Any hit, or any exception,
   blocks the whole reply: the user gets `BLOCKED_NOTICE`, never raw content.

Allowlist (`ReplyUrlPolicy`, from `TELEGRAM_REPLY_URL_ALLOWLIST`, default
empty): exact lowercase ASCII hosts. An allowed URL must be `https`, have no
userinfo, port, query, fragment or `%`-escape, and a short plain path; so even
an allowlisted host (open redirect, search query) cannot carry encoded data.

Top-level domains: any letter run (ASCII or not: `.\u0440\u0444`, `.\u4e2d\u56fd`, `.\u0938\u0902\u0917\u0920\u0928`,
`.\u307f\u3093\u306a`) or `xn--` counts, so IDN TLDs are caught; the cost is that some
non-URLs (`node.js`, `main.py`, `St.Louis`) are shown defanged.

IP forms (decided, pinned by tests): with any scheme every form is removed
(dotted, integer, hex, octal, IPv6). Without a scheme: dotted IPv4 is removed
(port/path) or defanged; bracketed IPv6 with a port or path is removed; bare
short/hex/octal/integer IPv4 (`127.1`, `0x7f000001`, `0177.0.0.1/x`) and bare
unbracketed IPv6 are left as text: no scheme and no TLD, so not auto-linked.

Input above `MAX_FILTER_CHARS` (8 Telegram messages) is blocked unscanned.

Known limits: `@mentions`, `#hashtags` and `/commands` are left as they are
(they open Telegram entities, not attacker URLs). Look-alike letters are not
mapped to ASCII; a homoglyph host is still a host and is neutralised like any
other. NFKC can change the look of some characters (ligatures, super-scripts).
An ideographic full stop between two CJK characters is kept (it is prose
punctuation), so a fully CJK host written with `\u3002` (`\u4f8b\u5b50\u3002\u4e2d\u56fd`) is not
caught. Hosts spelled out in text (`evil dot com`, `evil[.]com`,
`e v i l . c o m`) are left: they are not clickable, but a user who retypes
one sends whatever data it holds; that social channel no filter closes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from src.gateway.render import MAX_MESSAGE_CHARS

MAX_FILTER_CHARS = 8 * MAX_MESSAGE_CHARS
LINK_REMOVED = "[link removed]"
IMAGE_REMOVED = "[image removed]"
BLOCKED_NOTICE = (
    "I held back that reply because it contained a link I am not allowed to send. "
    "Ask me again and I will answer without links."
)

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JOINERS = {"\u200c", "\u200d"}
_DOT_FOLD = re.compile(r"(?<=\w)[\u3002\uff61](?=\w)")
_CJK = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]"
)
_URL_AFTER_HOST = frozenset("/?#:")
_URL_ASCII_NEXT = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/?=&#%._~-"
)
_TRAIL = ".,;:!?)]}*_~"
_TAIL = r"[^\s<>\"'`]*"

_DANGEROUS = (
    "javascript|vbscript|data|file|blob|mailto|tel|sms|tg|intent|about|ftp|ftps|https?|"
    "wss?|news|nntp|irc|magnet|sip|xmpp|geo|callto|skype|market|ton|ipfs|view-source"
)
_SCHEME = r"(?<![a-z0-9])(?:[a-z][a-z0-9+.\-]{0,31}:[/\\]|(?:" + _DANGEROUS + r"):(?=\S))" + _TAIL
_IPV4 = (
    r"(?<!\w)(?P<ip>(?:\d{1,3}\.){3}\d{1,3})(?!\w|\.\d)(?P<iprest>(?::\d{1,5})?(?:[/?#\\]"
    + _TAIL
    + r")?)"
)
# TLD: `xn--`, or a letter followed by letters / digits / combining marks of
# any script. ASCII punctuation, Latin-1 symbols, general and CJK/full-width
# punctuation end it.
_TLD = (
    r"(?:xn--[a-z0-9\-]{1,59}|[^\W\d_]"
    r"[^\s!-/:-@\[-`{-~\u00a0-\u00bf\u2000-\u206f\u3000-\u303f\ufe30-\ufe4f\uff00-\uffef]{1,62})"
)
_IPV6 = r"(?P<ip6>\[[0-9a-f:.]{2,45}\](?::\d{1,5})?[/?#\\]" + _TAIL + r")"
_DOMAIN = (
    r"(?<!\w)(?P<host>(?:[\w\-]{1,63}\.){1,127}" + _TLD + r")(?![\w\-])"
    r"(?P<rest>(?::\d{1,5})?(?:[/?#\\]" + _TAIL + r")?)"
)
_LINK_ALTS = rf"(?P<scheme>{_SCHEME})|{_IPV6}|{_IPV4}|{_DOMAIN}"

_ALL_RE = re.compile(
    r"(?P<refdef>^[ \t]{0,3}\[[^\[\]\n]{1,1000}\]:[ \t]*\S[^\n]*)"
    r"|(?P<mdimg>!\[[^\[\]\n]{0,1000}\]\s?(?:\([^)\n]{0,2048}\)|\[[^\[\]\n]{0,1000}\]))"
    r"|(?P<htmlimg><img\b[^>]{0,2048}>)"
    r"|(?P<mdlink>\[(?P<label>[^\[\]\n]{0,1000})\]\((?P<target>[^)\n]{0,2048})\))"
    rf"|{_LINK_ALTS}",
    re.IGNORECASE | re.MULTILINE,
)
_LINK_RE = re.compile(_LINK_ALTS, re.IGNORECASE)

_ALLOWED_URL = re.compile(r"https://([a-z0-9.\-]{1,253})(/[A-Za-z0-9/._~\-]{0,255})?", re.ASCII)
_HOST_RE = re.compile(
    r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9\-]{0,61}[a-z0-9]",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class ReplyUrlPolicy:
    """Hosts a reply may link to. Empty (the default) means none."""

    hosts: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for h in self.hosts:
            if not isinstance(h, str) or not _HOST_RE.fullmatch(h):
                raise ValueError("invalid reply allowlist host")  # never echo the value

    def allows(self, url: str) -> bool:
        if not self.hosts:
            return False
        m = _ALLOWED_URL.fullmatch(url)
        return m is not None and m.group(1) in self.hosts


@dataclass(frozen=True, slots=True)
class FilterResult:
    text: str
    removed: int
    blocked: bool


def normalise(text: str) -> str:
    text = _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        if unicodedata.category(ch) == "Cf":
            # Keep ZWJ/ZWNJ only inside non-ASCII runs (emoji, Indic conjuncts):
            # there it cannot join pieces of an ASCII host or scheme.
            prev = text[i - 1] if i > 0 else "a"
            nxt = text[i + 1] if i + 1 < n else "a"
            if not (ch in _JOINERS and ord(prev) > 0x7F and ord(nxt) > 0x7F):
                continue
        out.append(ch)
    # Fold an ideographic full stop used as a host dot; keep it between two
    # CJK characters, where it is sentence punctuation (see `_fold_dot`).
    return _DOT_FOLD.sub(_fold_dot, "".join(out))


def _fold_dot(m: re.Match[str]) -> str:
    s, i = m.string, m.start()
    if _CJK.match(s, i - 1) and _CJK.match(s, i + 1):
        # Sentence punctuation unless the CJK run after it is followed by a URL
        # delimiter and then an ASCII URL character: `例子。中国/x` and
        # `例子。中国:8080` are hosts; `你是谁?`, `注意:明天`, `中国/台湾` (NFKC
        # has already folded full-width `？` `：`) are prose.
        j = i + 1
        while j < len(s) and _CJK.match(s, j):
            j += 1
        if not (j + 1 < len(s) and s[j] in _URL_AFTER_HOST and s[j + 1] in _URL_ASCII_NEXT):
            return m.group(0)
    return "."


def _split_trail(token: str) -> tuple[str, str]:
    core = token.rstrip(_TRAIL)
    return core, token[len(core) :]


def _defang(host: str) -> str:
    return host.replace(".", "[.]")


def _neutralise(text: str, policy: ReplyUrlPolicy) -> tuple[str, int]:
    count = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal count
        if m.group("refdef") is not None:
            count += 1
            return LINK_REMOVED
        if m.group("mdimg") is not None or m.group("htmlimg") is not None:
            count += 1
            return IMAGE_REMOVED
        if m.group("mdlink") is not None:
            label, n = _neutralise(m.group("label"), policy)
            count += n + 1
            parts = m.group("target").strip().strip("<>").split()
            url = parts[0].strip("<>") if parts else ""
            label = label.strip()
            if url and policy.allows(url):
                return f"{label} ({url})" if label else url
            return f"{label} {LINK_REMOVED}" if label else LINK_REMOVED
        if m.group("scheme") is not None:
            core, trail = _split_trail(m.group("scheme"))
            if policy.allows(core):
                return m.group(0)
            count += 1
            return LINK_REMOVED + trail
        if m.group("ip6") is not None:
            count += 1
            return LINK_REMOVED + _split_trail(m.group("ip6"))[1]
        if m.group("ip") is not None:
            core, trail = _split_trail(m.group("iprest"))
            count += 1
            if core:
                return LINK_REMOVED + trail
            return _defang(m.group("ip")) + trail
        host = m.group("host")
        core, trail = _split_trail(m.group("rest"))
        if policy.allows("https://" + host + core):
            return m.group(0)
        count += 1
        if core:
            return LINK_REMOVED + trail
        return _defang(host) + trail

    return _ALL_RE.sub(sub, text), count


def find_links(text: str, policy: ReplyUrlPolicy) -> list[str]:
    """Independent post-check: kinds ("scheme", "ip", "domain") of every link
    Telegram could make clickable that the policy does not allow. Never returns
    the matched text (it may hold the data being exfiltrated)."""
    hits: list[str] = []
    for m in _LINK_RE.finditer(normalise(text)):
        if m.group("scheme") is not None:
            if not policy.allows(_split_trail(m.group("scheme"))[0]):
                hits.append("scheme")
        elif m.group("ip6") is not None or m.group("ip") is not None:
            hits.append("ip")
        else:
            core = _split_trail(m.group("rest"))[0]
            if not policy.allows("https://" + m.group("host") + core):
                hits.append("domain")
    return hits


def filter_reply(text: str, policy: ReplyUrlPolicy) -> FilterResult:
    """Fail closed: any post-check hit or internal error blocks the reply."""
    if len(text) > MAX_FILTER_CHARS:
        return FilterResult(BLOCKED_NOTICE, 0, True)  # bounded work, fail closed
    try:
        clean, removed = _neutralise(normalise(text), policy)
        if find_links(clean, policy):
            return FilterResult(BLOCKED_NOTICE, removed, True)
        return FilterResult(clean, removed, False)
    except Exception:  # noqa: BLE001 - fail closed, content never surfaced
        return FilterResult(BLOCKED_NOTICE, 0, True)
