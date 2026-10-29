# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.8: read-only Drive client. Recorded fixtures through the src/net seam."""

from __future__ import annotations

import pytest

from src.integrations.google.auth import SCOPE_DRIVE_READONLY, GoogleReauthRequired
from src.integrations.google.drive_api import (
    MAX_CONTENT_BYTES,
    DriveError,
    DriveReader,
    build_query,
)
from src.net import EgressClient, EgressDenied, Service
from tests.unit.integrations.google.drive_helpers import (
    ID_BIG,
    ID_DOC,
    ID_PDF,
    TOKEN,
    FakeAuth,
    FakeDrive,
    fx,
)
from tests.unit.net.net_helpers import RecordingTransport, make_settings


async def test_search_parses_and_drops_invalid_ids() -> None:
    drive = FakeDrive({"/drive/v3/files": (200, fx("drive_list.json"))})
    files = await drive.reader().search("budget")
    assert [f.file_id for f in files][:2] == [ID_DOC, ID_PDF]
    assert all("/" not in f.file_id and "." not in f.file_id for f in files)
    assert len(files) == 3  # the path-traversal id was dropped


async def test_requests_are_get_only_with_bearer_and_scope() -> None:
    drive = FakeDrive({"/drive/v3/files": (200, fx("drive_list.json"))})
    auth = FakeAuth()
    await drive.reader(auth).search("x")
    (req,) = drive.requests
    assert req.method == "GET" and req.url.host == "www.googleapis.com"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert auth.required == [[SCOPE_DRIVE_READONLY]]


async def test_google_doc_is_exported_as_text() -> None:
    drive = FakeDrive(
        {
            f"/drive/v3/files/{ID_DOC}": (200, fx("drive_meta_doc.json")),
            f"/drive/v3/files/{ID_DOC}/export": (200, b"hello doc"),
        }
    )
    r = drive.reader()
    data = await r.read_text(await r.metadata(ID_DOC))
    assert data == b"hello doc"
    assert drive.requests[-1].url.params["mimeType"] == "text/plain"


async def test_binary_is_refused_without_download() -> None:
    drive = FakeDrive({f"/drive/v3/files/{ID_PDF}": (200, fx("drive_meta_pdf.json"))})
    r = drive.reader()
    f = await r.metadata(ID_PDF)
    with pytest.raises(DriveError) as ei:
        await r.read_text(f)
    assert ei.value.code == "unsupported_type"
    assert len(drive.requests) == 1  # metadata only


async def test_oversized_known_size_refused_before_download() -> None:
    drive = FakeDrive({f"/drive/v3/files/{ID_BIG}": (200, fx("drive_meta_big.json"))})
    r = drive.reader()
    f = await r.metadata(ID_BIG)
    with pytest.raises(DriveError) as ei:
        await r.read_text(f)
    assert ei.value.code == "too_large" and len(drive.requests) == 1


async def test_oversized_body_is_cut_off_by_max_bytes() -> None:
    drive = FakeDrive(
        {
            f"/drive/v3/files/{ID_DOC}": (200, fx("drive_meta_doc.json")),
            f"/drive/v3/files/{ID_DOC}/export": (200, b"a" * (MAX_CONTENT_BYTES + 1)),
        }
    )
    r = drive.reader()
    with pytest.raises(DriveError) as ei:
        await r.read_text(await r.metadata(ID_DOC))
    assert ei.value.code == "too_large"


@pytest.mark.parametrize("fid", ["../about", "abc", "a/b/c/d/e/f/g/h/i/j", "x" * 65, "x" * 200, ""])
async def test_bad_file_id_never_reaches_the_network(fid: str) -> None:
    drive = FakeDrive({})
    with pytest.raises(DriveError) as ei:
        await drive.reader().metadata(fid)
    assert ei.value.code == "bad_id" and drive.requests == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "reauth_required"), (403, "not_found"), (404, "not_found"), (500, "unavailable")],
)
async def test_http_errors_map_to_fixed_codes(status: int, code: str) -> None:
    drive = FakeDrive({"/drive/v3/files": (status, b'{"error": "secret detail"}')})
    with pytest.raises(DriveError) as ei:
        await drive.reader().search("")
    assert ei.value.code == code and "secret" not in str(ei.value)


async def test_reauth_required_from_auth_is_reported() -> None:
    class Dead:
        async def access_token(self, required_scopes: object = ()) -> object:
            raise GoogleReauthRequired("invalid_grant")

    drive = FakeDrive({})
    with pytest.raises(DriveError) as ei:
        await DriveReader(
            EgressClient(make_settings(), transport=drive.transport),
            Dead(),  # type: ignore[arg-type]
        ).search("")
    assert ei.value.code == "reauth_required" and drive.requests == []


def test_query_escapes_quotes_and_backslashes() -> None:
    import re

    q = build_query("a' or name contains 'b\\")
    # drop escaped pairs (backslash + any char); only the 4 literal delimiters stay bare
    assert re.sub(r"\\.", "", q).count("'") == 4


def test_query_drops_control_chars_and_caps_length() -> None:
    assert "\n" not in build_query("a\nb\x00c")
    assert len(build_query("z" * 5000)) < 600
    assert build_query("") == "trashed = false"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path",
    [
        "/drive/v3/files",
        f"/drive/v3/files/{ID_DOC}",
        f"/drive/v3/files/{ID_DOC}/permissions",
        f"/drive/v3/files/{ID_DOC}/copy",
        "/upload/drive/v3/files",
        "/drive/v2/files/x/trash",
        "/%64rive/v3/files",
        "/DRIVE/v3/files",
        "/resumable/upload/drive/v3/files",
        "/upload/drive/v3beta/files",
        "/drive/v3beta/files",
        "/drive/v4/files",
    ],
)
async def test_net_refuses_every_drive_write(method: str, path: str) -> None:
    t = RecordingTransport()
    async with EgressClient(make_settings(), transport=t) as c:
        with pytest.raises(EgressDenied):
            await c.request(Service.GOOGLE_APIS, method, path)
    assert t.calls == []


async def test_net_still_allows_drive_get() -> None:
    t = RecordingTransport()
    async with EgressClient(make_settings(), transport=t) as c:
        await c.request(Service.GOOGLE_APIS, "GET", "/drive/v3/files")
    assert len(t.calls) == 1


def test_client_has_no_write_methods() -> None:
    public = {n for n in dir(DriveReader) if not n.startswith("_")}
    assert public == {"search", "metadata", "read_text"}


async def test_hostile_mime_and_time_are_normalised_in_the_client() -> None:
    drive = FakeDrive({"/drive/v3/files": (200, fx("drive_list_hostile.json"))})
    files = await drive.reader().search("")
    assert files[1].mime_type == "application/octet-stream"
    assert files[2].mime_type == "application/octet-stream"
    assert files[0].modified == "" and files[0].size is None
