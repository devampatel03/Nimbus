# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Gateway value types. No chat ids: the gateway is bound to the one
allowlisted user (R2), so a message is addressed by `message_id` alone."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class UpdateKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    DOCUMENT = "document"
    CALLBACK = "callback"
    # Never yielded: recorded for audit / offset advance only.
    DROPPED = "dropped"
    UNSUPPORTED = "unsupported"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class FileRef:
    file_id: str = field(repr=False)
    file_unique_id: str
    size: int | None
    mime_type: str | None
    file_name: str | None
    kind: UpdateKind  # VOICE or DOCUMENT


@dataclass(frozen=True, slots=True)
class MessageRef:
    message_id: int


@dataclass(frozen=True, slots=True)
class InboundUpdate:
    update_id: int
    kind: UpdateKind
    message_id: int | None = None
    text: str | None = None
    file: FileRef | None = None
    callback_id: str | None = field(default=None, repr=False)
    # Approval tokens and step-up keypad digits: never in a repr / log line.
    callback_data: str | None = field(default=None, repr=False)
    # True when someone other than the user authored the content (forward,
    # Recorder transcript document): ingest must treat it as `untrusted`.
    untrusted: bool = False
    # Telegram `from.id` (code-derived, set by `parse_update`). The approvals
    # layer re-checks it against the allowlist and binds it into the MAC.
    from_user_id: int | None = None


@dataclass(frozen=True, slots=True)
class ApprovalCard:
    """`text` is the exact payload the user is approving; it is shown verbatim
    (escaped) and never truncated. `approve_data`/`reject_data` are opaque
    signed tokens built by the approvals layer (<= 64 bytes, ADR-0009); the
    payload itself lives server-side in `pending_action`."""

    text: str
    approve_data: str = field(repr=False)
    reject_data: str = field(repr=False)


class TelegramError(Exception):
    """Sanitised Bot API failure. Never carries the token or request URL."""

    def __init__(self, reason: str, *, code: int | None = None) -> None:
        self.reason = reason
        self.code = code
        super().__init__(f"telegram {reason}" + (f" code={code}" if code is not None else ""))


class TelegramAuthError(TelegramError):
    """401/404 from the Bot API: the token is wrong. Not retryable."""


class TelegramRateLimited(TelegramError):
    """429 persisted past our retry budget (or retry_after too long)."""


class FileTooLarge(TelegramError):
    pass


class MessageTooLong(TelegramError):
    """An approval payload cannot be shown in full in one message."""


class OutboundBlocked(TelegramError):
    """The exfil filter (P4.3) refused a code-built text (keypad prompt)."""
