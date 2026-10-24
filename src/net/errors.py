# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Egress error types (INV-7-safe).

Every message is built only from the closed set of values passed to
`EgressError.__init__`: a `Service` name, an HTTP method, and a fixed reason
code. URLs, paths, query strings, headers and bodies are never interpolated,
because Telegram puts the bot token in the path and Google/Notion put bearer
tokens in headers. Callers re-raising these must not add those either.
"""

from __future__ import annotations


class EgressError(Exception):
    """Base class. `reason` is a short, fixed, machine-readable code."""

    def __init__(self, reason: str, *, service: str | None = None, method: str | None = None):
        self.reason = reason
        self.service = service
        self.method = method
        parts = [f"egress {reason}"]
        if service is not None:
            parts.append(f"service={service}")
        if method is not None:
            parts.append(f"method={method}")
        super().__init__(" ".join(parts))


class EgressDenied(EgressError):
    """Policy refusal. Always raised BEFORE any network I/O for the request."""


class EgressRedirectBlocked(EgressDenied):
    """A response redirected off the originating host, or to non-https."""


class EgressResponseTooLarge(EgressError):
    """The response body exceeded the cap; the stream was abandoned."""


class EgressTimeout(EgressError):
    """Connect/read/write/pool or overall deadline exceeded."""


class EgressTransportError(EgressError):
    """Any other transport failure (DNS, TLS, connection reset, ...)."""


class EgressConfigError(ValueError):
    """Invalid net settings (non-https LLM URL, bad hostname, ...)."""
