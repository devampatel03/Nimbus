# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""R13 (P3.5, ADR-0019 addendum): only the primary calendar's events collection
and one event by strict id are reachable; every denial makes zero transport calls."""

from __future__ import annotations

import pytest

from src.net import EgressClient, EgressDenied, Service
from tests.unit.net.net_helpers import RecordingTransport, make_settings

EV = "/calendar/v3/calendars/primary/events"


def _client() -> tuple[EgressClient, RecordingTransport]:
    t = RecordingTransport()
    return EgressClient(make_settings(), transport=t), t


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", EV),
        ("POST", EV),
        ("GET", f"{EV}/abcde12345"),
        ("PATCH", f"{EV}/abcde12345"),
        ("DELETE", f"{EV}/abcde12345"),
    ],
)
async def test_allowed_calendar_requests_reach_transport(method: str, path: str) -> None:
    client, t = _client()
    async with client:
        resp = await client.request(Service.GOOGLE_APIS, method, path)
    assert resp.status_code == 200 and len(t.calls) == 1


@pytest.mark.parametrize(
    ("method", "path", "reason"),
    [
        ("POST", f"{EV}/quickAdd", "google_calendar_path"),
        ("POST", f"{EV}/import", "google_calendar_path"),
        ("POST", f"{EV}/abcde12345/move", "google_calendar_path"),
        ("POST", f"{EV}/watch", "google_calendar_path"),
        ("GET", f"{EV}/abcde12345/instances", "google_calendar_path"),
        ("GET", "/calendar/v3/calendars/other%40example.com/events", "google_calendar_path"),
        ("GET", "/calendar/v3/calendars/primary/acl", "google_calendar_path"),
        ("GET", "/calendar/v3/users/me/settings", "google_calendar_path"),
        ("GET", "/calendar/v3/calendars/primary", "google_calendar_path"),
        ("DELETE", "/calendar/v3/calendars/primary", "google_calendar_path"),
        ("GET", f"{EV}/", "google_calendar_path"),
        ("GET", f"{EV}/ABCDE12345", "google_calendar_path"),
        ("GET", f"{EV}/abc", "google_calendar_path"),
        ("GET", f"{EV}/{'a' * 65}", "google_calendar_path"),
        ("GET", "/upload/calendar/v3/calendars/primary/events", "google_calendar_path"),
        ("GET", "/calendar/v3/calendars/PRIMARY2/events", "google_calendar_path"),
        ("PUT", f"{EV}/abcde12345", "google_calendar_method"),
        ("HEAD", EV, "google_calendar_method"),
    ],
)
@pytest.mark.parametrize("service", [Service.GOOGLE_APIS, Service.GMAIL])
async def test_other_calendar_paths_and_methods_are_denied_before_io(
    service: Service, method: str, path: str, reason: str
) -> None:
    client, t = _client()
    async with client:
        with pytest.raises(EgressDenied) as ei:
            await client.request(service, method, path)
    assert ei.value.reason == reason
    assert t.calls == []
