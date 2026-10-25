# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Raw Bot API update -> `Parsed` verdict. Pure and total: any shape of
hostile JSON yields a verdict (or None when there is no usable `update_id`),
never an exception. The allowlist check (R2) lives here and applies to every
update kind, callbacks included."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.gateway.types import FileRef, InboundUpdate, UpdateKind


@dataclass(frozen=True, slots=True)
class Parsed:
    update_id: int
    from_user_id: int | None
    kind: UpdateKind
    update: InboundUpdate | None  # set only for deliverable kinds


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _sender(obj: Any) -> int | None:
    if not isinstance(obj, dict):
        return None
    frm = obj.get("from")
    return _int(frm.get("id")) if isinstance(frm, dict) else None


def _file_ref(obj: dict[str, Any], kind: UpdateKind) -> FileRef | None:
    file_id = _str(obj.get("file_id"))
    uid = _str(obj.get("file_unique_id"))
    if not file_id or not uid:
        return None
    return FileRef(
        file_id=file_id,
        file_unique_id=uid,
        size=_int(obj.get("file_size")),
        mime_type=_str(obj.get("mime_type")),
        file_name=_str(obj.get("file_name")),
        kind=kind,
    )


def _is_users_private_chat(chat: Any, allowed_user_id: int) -> bool:
    return (
        isinstance(chat, dict)
        and chat.get("type") == "private"
        and _int(chat.get("id")) == allowed_user_id
    )


# Anything here means someone other than the user (or a bot) authored/relayed
# the content (INV-6): treat as untrusted.
_THIRD_PARTY_KEYS = (
    "forward_origin",
    "forward_date",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "via_bot",
    "is_automatic_forward",
)


def _is_text_document(doc: dict[str, Any]) -> bool:
    name = (_str(doc.get("file_name")) or "").lower()
    mime = (_str(doc.get("mime_type")) or "").lower()
    return name.endswith(".txt") and mime in ("", "text/plain")


def parse_update(raw: Any, allowed_user_id: int) -> Parsed | None:
    if not isinstance(raw, dict):
        return None
    update_id = _int(raw.get("update_id"))
    if update_id is None or update_id < 0:
        return None

    message = raw.get("message")
    callback = raw.get("callback_query")
    if isinstance(message, dict):
        sender = _sender(message)
    elif isinstance(callback, dict):
        sender = _sender(callback)
    else:
        sender = next((s for v in raw.values() if (s := _sender(v)) is not None), None)

    if sender is None or sender != allowed_user_id:
        return Parsed(update_id, sender, UpdateKind.DROPPED, None)

    if isinstance(callback, dict):
        cb_id = _str(callback.get("id"))
        data = _str(callback.get("data"))
        msg = callback.get("message")
        mid = _int(msg.get("message_id")) if isinstance(msg, dict) else None
        if not cb_id or data is None:
            return Parsed(update_id, sender, UpdateKind.MALFORMED, None)
        # The button must live in the user's own private chat; inline-message
        # callbacks (no `message`) and anything else are not served.
        if not (isinstance(msg, dict) and _is_users_private_chat(msg.get("chat"), allowed_user_id)):
            return Parsed(update_id, sender, UpdateKind.DROPPED, None)
        return Parsed(
            update_id,
            sender,
            UpdateKind.CALLBACK,
            InboundUpdate(
                update_id,
                UpdateKind.CALLBACK,
                message_id=mid,
                callback_id=cb_id,
                callback_data=data,
            ),
        )

    if not isinstance(message, dict):
        return Parsed(update_id, sender, UpdateKind.UNSUPPORTED, None)

    if not _is_users_private_chat(message.get("chat"), allowed_user_id):
        return Parsed(update_id, sender, UpdateKind.DROPPED, None)

    mid = _int(message.get("message_id"))
    forwarded = any(k in message for k in _THIRD_PARTY_KEYS)

    voice = message.get("voice")
    document = message.get("document")
    text = _str(message.get("text"))
    if isinstance(voice, dict):
        ref = _file_ref(voice, UpdateKind.VOICE)
        if ref is None:
            return Parsed(update_id, sender, UpdateKind.MALFORMED, None)
        return Parsed(
            update_id,
            sender,
            UpdateKind.VOICE,
            InboundUpdate(
                update_id, UpdateKind.VOICE, message_id=mid, file=ref, untrusted=forwarded
            ),
        )
    if isinstance(document, dict):
        # Only plain-text documents (Pixel Recorder transcripts). Third-party
        # speech: always untrusted.
        if not _is_text_document(document):
            return Parsed(update_id, sender, UpdateKind.UNSUPPORTED, None)
        ref = _file_ref(document, UpdateKind.DOCUMENT)
        if ref is None:
            return Parsed(update_id, sender, UpdateKind.MALFORMED, None)
        return Parsed(
            update_id,
            sender,
            UpdateKind.DOCUMENT,
            InboundUpdate(
                update_id,
                UpdateKind.DOCUMENT,
                message_id=mid,
                text=_str(message.get("caption")),
                file=ref,
                untrusted=True,
            ),
        )
    if text is not None:
        return Parsed(
            update_id,
            sender,
            UpdateKind.TEXT,
            InboundUpdate(
                update_id, UpdateKind.TEXT, message_id=mid, text=text, untrusted=forwarded
            ),
        )
    return Parsed(update_id, sender, UpdateKind.UNSUPPORTED, None)
