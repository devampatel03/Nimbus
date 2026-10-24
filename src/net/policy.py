# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Pure, I/O-free egress policy: Service -> host allowlist, path validation,
method allowlist, header denylist, and method+path deny rules (R13, R6).

Everything here is deterministic and fails closed: anything not explicitly
recognised raises `EgressDenied`. Nothing here touches the network, so a
denial provably happens before any transport is invoked.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote

from src.net.errors import EgressDenied
from src.net.settings import NetSettings, host_from_https_url


class Service(StrEnum):
    TELEGRAM = "telegram"
    LLM = "llm"
    GOOGLE_OAUTH = "google_oauth"
    GOOGLE_APIS = "google_apis"
    GMAIL = "gmail"
    NOTION = "notion"
    HEALTHCHECK = "healthcheck"
    BACKUP = "backup"


# Fixed vendor hosts. Not overridable from env (see settings.py docstring).
FIXED_HOSTS: Mapping[Service, str] = {
    Service.TELEGRAM: "api.telegram.org",
    Service.GOOGLE_OAUTH: "oauth2.googleapis.com",
    Service.GOOGLE_APIS: "www.googleapis.com",
    Service.GMAIL: "gmail.googleapis.com",
    Service.NOTION: "api.notion.com",
}

ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})

# Caller-supplied headers that could re-route a request at a shared front end
# (domain fronting via Host on Google's GFE), smuggle a different target URL,
# or talk to a proxy. Compared lowercase.
FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "forwarded",
        "x-forwarded-host",
        "x-forwarded-for",
        "x-original-url",
        "x-rewrite-url",
        "x-http-method-override",
        "x-http-method",
        "x-method-override",
        "proxy-authorization",
        "proxy-connection",
        "connection",
        "transfer-encoding",
        "content-length",
    }
)

MAX_PATH_LEN = 2048
_MAX_UNQUOTE_ROUNDS = 4
_CTRL_RE = re.compile(r"[\x00-\x20\x7f]")
_HEADER_CTRL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # tab allowed in values


@dataclass(frozen=True)
class Allowlist:
    """Resolved Service -> host map for one process. Immutable."""

    hosts: Mapping[Service, str]

    @classmethod
    def from_settings(cls, settings: NetSettings) -> Allowlist:
        hosts: dict[Service, str] = dict(FIXED_HOSTS)
        if settings.llm_base_url is not None:
            hosts[Service.LLM] = host_from_https_url(settings.llm_base_url)
        if settings.healthcheck_host is not None:
            hosts[Service.HEALTHCHECK] = settings.healthcheck_host
        if settings.backup_host is not None:
            hosts[Service.BACKUP] = settings.backup_host
        if settings.enabled_services is not None:
            # Unknown names are ignored: they can only ever narrow the set.
            enabled = {s for s in Service if s.value in settings.enabled_services}
            hosts = {s: h for s, h in hosts.items() if s in enabled}
        return cls(hosts=dict(hosts))

    def host_for(self, service: object) -> str:
        if not isinstance(service, Service):
            raise EgressDenied("unknown_service")
        host = self.hosts.get(service)
        if host is None:
            raise EgressDenied("service_disabled", service=service.value)
        return host

    def service_for_host(self, host: str) -> Service | None:
        for service, h in self.hosts.items():
            if h == host:
                return service
        return None


# --------------------------------------------------------------------------
# Deny rules (R6 and friends). A rule gets (service, METHOD, normalised path)
# and returns a reason code to deny, or None. Defaults are always applied;
# callers can only add rules, never remove them.
# --------------------------------------------------------------------------

DenyRule = Callable[[Service, str, str], "str | None"]

# The default rules are deliberately host-agnostic. Gmail is reachable on
# gmail.googleapis.com and www.googleapis.com (and under /upload/...), and any
# Google-fronted host (oauth2.googleapis.com, or an LLM endpoint such as
# generativelanguage.googleapis.com configured as Service.LLM) may route by
# path at the shared front end. These path shapes are not used by any other
# allowlisted service, so matching them everywhere costs nothing.
#
# ':' is treated as a separator (custom-method syntax) and the verb must not
# be followed by another name character, so 'send', 'send/', 'send:x' and
# 'send.json' all match while 'sendAs' (a settings resource) is left to the
# settings rule.
_GMAIL_SEND_RE = re.compile(
    r"/gmail/v\d+/users/[^/]+/(?:messages[/:](?:send|import|insert)|drafts[/:]send)"
    r"(?![a-z0-9_-])"
)
_GMAIL_SETTINGS_RE = re.compile(r"/gmail/v\d+/users/[^/]+/settings(?![a-z0-9_-])")
# Google batch endpoints multiplex arbitrary inner requests (incl. send) in
# a multipart body the path check cannot see.
_GOOGLE_BATCH_RE = re.compile(r"^/batch(?![a-z0-9_-])")


def deny_gmail_send(service: Service, method: str, path: str) -> str | None:
    if _GMAIL_SEND_RE.search(path):
        return "gmail_send_path"
    return None


def deny_gmail_settings_write(service: Service, method: str, path: str) -> str | None:
    # Forwarding addresses / filters with a forward action are send-equivalent.
    if method not in ("GET", "HEAD") and _GMAIL_SETTINGS_RE.search(path):
        return "gmail_settings_write"
    return None


def deny_google_batch(service: Service, method: str, path: str) -> str | None:
    if _GOOGLE_BATCH_RE.search(path):
        return "google_batch"
    return None


DEFAULT_DENY_RULES: tuple[DenyRule, ...] = (
    deny_gmail_send,
    deny_gmail_settings_write,
    deny_google_batch,
)


def normalise_path(path: object, *, service: Service, method: str) -> str:
    """Validate a caller path and return its fully-decoded lowercase form for
    rule matching. The ORIGINAL string is what gets sent; it is only accepted
    if its decoded form is clean, so the two cannot disagree in a way that
    matters (no dot-segments, no encoded slashes hiding a denied path)."""

    def deny(reason: str) -> EgressDenied:
        return EgressDenied(reason, service=service.value, method=method)

    if not isinstance(path, str) or not path:
        raise deny("bad_path")
    if len(path) > MAX_PATH_LEN:
        raise deny("path_too_long")
    if not path.startswith("/") or path.startswith("//"):
        raise deny("bad_path")
    decoded = path
    for _ in range(_MAX_UNQUOTE_ROUNDS):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    else:
        raise deny("bad_path_encoding")
    # ';' is rejected so servlet-style path parameters ('send;x') cannot hide
    # a denied verb from the rule regexes.
    if _CTRL_RE.search(decoded) or any(c in decoded for c in "\\?#;"):
        raise deny("bad_path")
    if "//" in decoded:
        raise deny("bad_path")
    segments = decoded.split("/")
    if any(seg in (".", "..") for seg in segments):
        raise deny("bad_path")
    return decoded.lower()


def check_method(method: object, *, service: Service) -> str:
    if not isinstance(method, str) or method.upper() not in ALLOWED_METHODS:
        raise EgressDenied("method_not_allowed", service=service.value)
    return method.upper()


def check_headers(headers: Mapping[str, str] | None, *, service: Service, method: str) -> None:
    if not headers:
        return
    for name, value in headers.items():
        lname = name.lower()
        if lname in FORBIDDEN_HEADERS or lname.startswith("proxy-"):
            raise EgressDenied("forbidden_header", service=service.value, method=method)
        if not isinstance(value, str) or _HEADER_CTRL_RE.search(value) or _CTRL_RE.search(name):
            raise EgressDenied("bad_header_value", service=service.value, method=method)


def apply_deny_rules(
    rules: Iterable[DenyRule], *, service: Service, method: str, normalised_path: str
) -> None:
    for rule in rules:
        reason = rule(service, method, normalised_path)
        if reason is not None:
            raise EgressDenied(reason, service=service.value, method=method)


def all_rules(extra: Sequence[DenyRule] = ()) -> tuple[DenyRule, ...]:
    return DEFAULT_DENY_RULES + tuple(extra)
