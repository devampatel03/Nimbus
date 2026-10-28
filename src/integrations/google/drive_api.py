# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Read-only Google Drive client (P3.8; ADR-0019, TARGET_DESIGN Q4, H17).

Scope: ``drive.readonly`` only. This module can issue exactly three GETs on
``www.googleapis.com`` (``Service.GOOGLE_APIS``, no new host): list files,
read one file's metadata, and fetch one file's text (``alt=media`` for plain
text types, ``/export`` for Google Docs/Sheets/Slides). There is no create,
update, delete, copy, share or upload method, and ``src.net`` additionally
refuses any non-GET on ``/drive/...`` (``deny_drive_write``).

Everything returned is UNTRUSTED (file names, owners, contents). Callers wrap
it in ``UntrustedText``; nothing here is ever shown to the model directly.
Sizes are capped before and during download (``max_bytes``), the MIME type
must be on an allow-list (binary is refused), and ids are validated so a model
or document cannot steer a request to another path.

R15: live use needs the consent screen "In production" (see ``auth.py``, H4).
Single attempt per call, no retry: a transient failure is reported to the user.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from pydantic import SecretStr

from src.integrations.google.auth import SCOPE_DRIVE_READONLY, GoogleAuthError
from src.net import EgressClient, EgressDenied, EgressError, EgressResponseTooLarge, Service

FILE_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{10,128}")
MAX_LIST_RESULTS: Final = 10
MAX_QUERY_CHARS: Final = 200
MAX_CONTENT_BYTES: Final = 256 * 1024
MAX_META_BYTES: Final = 256 * 1024
REQUEST_TIMEOUT_S: Final = 20.0

# Downloaded as-is (alt=media).
PLAIN_TYPES: Final = frozenset(
    {"text/plain", "text/markdown", "text/csv", "text/tab-separated-values", "application/json"}
)
# Google-native types, exported as text. Value = export MIME type.
EXPORT_TYPES: Final = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
READABLE_TYPES: Final = PLAIN_TYPES | frozenset(EXPORT_TYPES)
_META_FIELDS: Final = "id,name,mimeType,size,modifiedTime,owners(displayName,emailAddress)"
_MIME_RE: Final = re.compile(r"[a-z0-9.+-]{1,40}/[a-z0-9.+-]{1,80}")
_TIME_RE: Final = re.compile(r"[0-9T:.\-Z+]{10,40}")


class DriveError(Exception):
    """Fixed-code failure; the message is the code only (INV-7)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AccessTokens(Protocol):
    async def access_token(self, required_scopes: Iterable[str] = ()) -> SecretStr: ...


@dataclass(frozen=True)
class DriveFile:
    """Raw (untrusted) metadata. `file_id`, `mime_type`, `size` and `modified`
    are validated shapes; `name` and `owners` are attacker-controlled text."""

    file_id: str
    name: str
    mime_type: str
    size: int | None
    modified: str
    owners: tuple[str, ...]


def escape_query_term(term: str) -> str:
    """Escape for a Drive `q` single-quoted string literal."""
    return term.replace("\\", "\\\\").replace("'", "\\'")


def build_query(text: str) -> str:
    """Name/full-text match on the term, never trashed. Non-printable characters
    are dropped and length is capped; quotes are escaped, so the term cannot end
    the literal and inject query operators."""
    cleaned = "".join(c for c in text if c.isprintable())[:MAX_QUERY_CHARS].strip()
    q = "trashed = false"
    if cleaned:
        t = escape_query_term(cleaned)
        q += f" and (name contains '{t}' or fullText contains '{t}')"
    return q


def _parse_file(raw: Any) -> DriveFile | None:
    if not isinstance(raw, dict):
        return None
    fid, name, mime = raw.get("id"), raw.get("name"), raw.get("mimeType")
    if not (isinstance(fid, str) and FILE_ID_RE.fullmatch(fid) and isinstance(name, str)):
        return None
    if not (isinstance(mime, str) and _MIME_RE.fullmatch(mime)):
        mime = "application/octet-stream"
    size_raw = raw.get("size")
    size = int(size_raw) if isinstance(size_raw, str) and size_raw.isdigit() else None
    mod = raw.get("modifiedTime")
    modified = mod if isinstance(mod, str) and _TIME_RE.fullmatch(mod) else ""
    owners: list[str] = []
    for o in raw.get("owners") or []:
        if isinstance(o, dict):
            owners.append(str(o.get("displayName") or o.get("emailAddress") or "")[:100])
    return DriveFile(fid, name[:300], mime, size, modified, tuple(owners[:3]))


class DriveReader:
    """Read-only Drive access through the egress client."""

    def __init__(self, egress: EgressClient, auth: AccessTokens) -> None:
        self._egress = egress
        self._auth = auth

    async def _get(self, path: str, params: dict[str, str], max_bytes: int) -> bytes:
        try:
            token = await self._auth.access_token([SCOPE_DRIVE_READONLY])
        except GoogleAuthError as exc:
            raise DriveError("reauth_required" if exc.needs_reauth else "auth_failed") from None
        try:
            resp = await self._egress.request(
                Service.GOOGLE_APIS,
                "GET",
                path,
                params=params,
                headers={"authorization": f"Bearer {token.get_secret_value()}"},
                timeout_s=REQUEST_TIMEOUT_S,
                max_bytes=max_bytes,
            )
        except EgressResponseTooLarge:
            raise DriveError("too_large") from None
        except EgressDenied:
            raise DriveError("egress_denied") from None
        except EgressError:
            raise DriveError("unavailable") from None
        if resp.status_code == 401:
            raise DriveError("reauth_required")
        if resp.status_code in (403, 404):
            raise DriveError("not_found")
        if not 200 <= resp.status_code < 300:
            raise DriveError("unavailable")
        return resp.content

    @staticmethod
    def _json(content: bytes) -> Any:
        try:
            return json.loads(content)
        except ValueError:
            raise DriveError("bad_response") from None

    async def search(self, text: str) -> list[DriveFile]:
        data = self._json(
            await self._get(
                "/drive/v3/files",
                {
                    "q": build_query(text),
                    "pageSize": str(MAX_LIST_RESULTS),
                    "orderBy": "modifiedTime desc",
                    "fields": f"files({_META_FIELDS})",
                    "spaces": "drive",
                },
                MAX_META_BYTES,
            )
        )
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            raise DriveError("bad_response")
        parsed = (_parse_file(f) for f in files[:MAX_LIST_RESULTS])
        return [f for f in parsed if f is not None]

    async def metadata(self, file_id: str) -> DriveFile:
        if not FILE_ID_RE.fullmatch(file_id):
            raise DriveError("bad_id")
        data = self._json(
            await self._get(f"/drive/v3/files/{file_id}", {"fields": _META_FIELDS}, MAX_META_BYTES)
        )
        f = _parse_file(data)
        if f is None or f.file_id != file_id:
            raise DriveError("bad_response")
        return f

    async def read_text(self, f: DriveFile) -> bytes:
        """Bytes of a readable file (<= MAX_CONTENT_BYTES). Refuses binary and
        oversized files before downloading where the size is known."""
        if f.mime_type not in READABLE_TYPES:
            raise DriveError("unsupported_type")
        if f.size is not None and f.size > MAX_CONTENT_BYTES:
            raise DriveError("too_large")
        if f.mime_type in EXPORT_TYPES:
            path = f"/drive/v3/files/{f.file_id}/export"
            params = {"mimeType": EXPORT_TYPES[f.mime_type]}
        else:
            path = f"/drive/v3/files/{f.file_id}"
            params = {"alt": "media"}
        return await self._get(path, params, MAX_CONTENT_BYTES)
