# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Names round-trip through opaque per-session refs (SCR-3).

`create_ref` -> a real name never leaves the local process in cleartext
via a ref; `resolve_ref` -> the same session gets the name back.
"""

import uuid

import pytest

from src.security import scrubber


def _session_id() -> str:
    return f"sess-{uuid.uuid4()}"


def test_create_ref_returns_opaque_token_not_the_name():
    session_id = _session_id()
    ref = scrubber.create_ref(session_id, "person", "Rohit Sharma")
    assert "Rohit" not in ref
    assert "Sharma" not in ref
    assert ref.startswith("<person_")
    assert ref.endswith(">")


def test_ref_round_trips_back_to_the_canonical_name():
    session_id = _session_id()
    ref = scrubber.create_ref(session_id, "person", "Rohit Sharma")
    assert scrubber.resolve_ref(session_id, ref) == "Rohit Sharma"


def test_create_ref_is_idempotent_per_session():
    session_id = _session_id()
    ref1 = scrubber.create_ref(session_id, "person", "Rohit Sharma")
    ref2 = scrubber.create_ref(session_id, "person", "Rohit Sharma")
    assert ref1 == ref2


def test_refs_are_scoped_per_session_not_global():
    name = "Rohit Sharma"
    ref_a = scrubber.create_ref(_session_id(), "person", name)
    ref_b = scrubber.create_ref(_session_id(), "person", name)
    assert ref_a != ref_b, "refs must be non-sequential/session-scoped, not derived from the name"


def test_resolve_ref_unknown_returns_none():
    assert scrubber.resolve_ref(_session_id(), "<person_deadbeef>") is None


def test_create_ref_rejects_unknown_entity_type():
    with pytest.raises(ValueError):
        scrubber.create_ref(_session_id(), "vehicle", "Rohit Sharma")


def test_substitute_known_entities_replaces_registered_name_in_params():
    session_id = _session_id()
    ref = scrubber.create_ref(session_id, "person", "Rohit Sharma")
    params = {"notes": "Follow up with Rohit Sharma about the proposal"}
    substituted = scrubber._substitute_known_entities(session_id, params)
    assert "Rohit Sharma" not in substituted["notes"]
    assert ref in substituted["notes"]


def test_substitute_known_entities_leaves_unregistered_text_untouched():
    session_id = _session_id()
    params = {"notes": "Unrelated text with no registered entities"}
    substituted = scrubber._substitute_known_entities(session_id, params)
    assert substituted == params


def test_resolve_refs_in_value_resolves_ref_back_to_name():
    session_id = _session_id()
    ref = scrubber.create_ref(session_id, "person", "Rohit Sharma")
    resolved = scrubber._resolve_refs_in_value(session_id, f"Confirmed with {ref}")
    assert resolved == "Confirmed with Rohit Sharma"


def test_resolve_refs_in_value_passes_through_non_string():
    session_id = _session_id()
    assert scrubber._resolve_refs_in_value(session_id, 42) == 42
    assert scrubber._resolve_refs_in_value(session_id, None) is None
