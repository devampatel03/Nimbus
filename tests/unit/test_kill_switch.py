# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""SCR-6: with the kill switch engaged, execute_external raises and never
touches the network; the rest of the local system keeps working.
"""

import uuid

import pytest

from src.contracts.tool_intent import RiskClass, ToolIntent
from src.security import kill_switch, pii_detector, scrubber


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    kill_switch.disengage()
    yield
    kill_switch.disengage()


def _intent() -> ToolIntent:
    return ToolIntent(
        tool="calendar",
        action="create",
        params={"title": "Follow-up"},
        entity_refs={},
        risk_class=RiskClass.WRITE_REVERSIBLE,
    )


async def test_execute_external_raises_when_kill_switch_engaged():
    kill_switch.engage()
    with pytest.raises(kill_switch.KillSwitchEngagedError):
        await scrubber.execute_external(_intent(), session_id=f"sess-{uuid.uuid4()}")


async def test_execute_external_does_not_call_network_when_engaged(monkeypatch):
    called = False

    async def _fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be touched while kill switch is engaged")

    monkeypatch.setattr("httpx.AsyncClient.post", _fail_if_called)
    kill_switch.engage()
    with pytest.raises(kill_switch.KillSwitchEngagedError):
        await scrubber.execute_external(_intent(), session_id=f"sess-{uuid.uuid4()}")
    assert called is False


def test_is_engaged_reflects_toggle_state():
    kill_switch.disengage()
    assert kill_switch.is_engaged() is False
    kill_switch.engage()
    assert kill_switch.is_engaged() is True
    kill_switch.disengage()
    assert kill_switch.is_engaged() is False


def test_rest_of_system_functions_while_kill_switch_engaged():
    """Local-only mode: everything that doesn't need cloud egress still works."""
    kill_switch.engage()

    # PII detection (pure local function) is unaffected.
    findings = pii_detector.detect_pii("contact me at a@b.com")
    assert findings

    # Entity ref creation/resolution (local, no network) is unaffected.
    session_id = f"sess-{uuid.uuid4()}"
    ref = scrubber.create_ref(session_id, "person", "Rohit Sharma")
    assert scrubber.resolve_ref(session_id, ref) == "Rohit Sharma"

    # Schema/registry validation (local, no network) is unaffected.
    scrubber._validate_intent(_intent())  # must not raise
