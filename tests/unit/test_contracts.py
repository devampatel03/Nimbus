# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Golden-fixture and field/type compatibility tests for src/contracts/.

Per docs/TECHNICAL_ARCHITECTURE.md section 12: every contract must have a
versioned schema, a golden-fixture test, and a compatibility test that
fails if a field is removed or its type changes. `EXPECTED_FIELDS` below
is that frozen record — amend it only alongside a deliberate, reviewed
contract change.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from src.contracts import (
    Event,
    MemoryQuery,
    MemoryResult,
    ProactiveCandidate,
    ToolIntent,
    ToolResult,
    Transcript,
    UserProfileSlice,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# model -> fixture file -> {field_name: annotation!r}
# Recorded once at freeze time; a field removal or type change breaks the
# corresponding assertion below.
EXPECTED_FIELDS: dict[type[BaseModel], dict[str, str]] = {
    Event: {
        "source_key": "str",
        "kind": "EventKind",
        "source": "str",
        "lane": "Literal['trusted', 'untrusted']",
        "confidence": "float",
        "body": "str | None",
        "meta": "dict[str, Any]",
        "file_unique_id": "str | None",
        "occurred_at": "datetime",
    },
    Transcript: {
        "segments": "list[Segment]",
        "source_model": "str",
        "language": "str | None",
        "untrusted": "bool",
        "created_at": "datetime",
    },
    MemoryQuery: {
        "user_id": "UUID",
        "query_text": "str",
        "top_k": "int",
        "active_entities": "list[str]",
        "present_speakers": "list[UUID]",
    },
    MemoryResult: {
        "memory_id": "UUID",
        "content": "str",
        "score": "float",
        "source_session": "UUID",
        "provenance": "str",
        "valid_from": "datetime",
        "valid_to": "datetime | None",
        "confidence": "float",
    },
    UserProfileSlice: {
        "user_id": "UUID",
        "traits": "list[TraitSummary]",
        "active_stances": "list[StanceSummary]",
        "generated_at": "datetime",
        "version": "int",
    },
    ToolIntent: {
        "tool": "str",
        "action": "str",
        "params": "dict[str, str | int | float | bool | None]",
        "entity_refs": "dict[str, str]",
        "risk_class": "RiskClass",
    },
    ToolResult: {
        "tool": "str",
        "action": "str",
        "status": "str",
        "result": "dict[str, str | int | float | bool | None]",
        "entity_refs": "dict[str, str]",
        "error_message": "str | None",
    },
    ProactiveCandidate: {
        "candidate_id": "UUID",
        "scanner": "str",
        "user_id": "UUID",
        "summary": "str",
        "expected_value": "float",
        "created_at": "datetime",
        "decay_at": "datetime | None",
        "status": "str",
    },
}

FIXTURE_FILES: dict[type[BaseModel], str] = {
    Event: "event.json",
    Transcript: "transcript.json",
    MemoryQuery: "memory_query.json",
    MemoryResult: "memory_result.json",
    UserProfileSlice: "user_profile_slice.json",
    ToolIntent: "tool_intent.json",
    ToolResult: "tool_result.json",
    ProactiveCandidate: "proactive_candidate.json",
}


_MODULE_PREFIX = re.compile(r"\b(?:[a-zA-Z_][a-zA-Z0-9_]*\.)+([A-Za-z_][A-Za-z0-9_]*)\b")


def _annotation_repr(annotation: Any) -> str:
    if isinstance(annotation, type):
        return annotation.__name__
    # Strip fully-qualified module prefixes (e.g. "pkg.mod.Utterance" -> "Utterance")
    # so the recorded expectation doesn't churn on internal module moves.
    return _MODULE_PREFIX.sub(r"\1", str(annotation))


@pytest.mark.parametrize("model", list(EXPECTED_FIELDS))
def test_no_field_removed_or_retyped(model: type[BaseModel]) -> None:
    """A field missing here, or with a changed annotation, breaks a consumer pod."""
    expected = EXPECTED_FIELDS[model]
    actual = {
        name: _annotation_repr(field.annotation) for name, field in model.model_fields.items()
    }

    missing = expected.keys() - actual.keys()
    assert not missing, f"{model.__name__} is missing frozen fields: {missing}"

    for field_name, expected_type in expected.items():
        assert actual[field_name] == expected_type, (
            f"{model.__name__}.{field_name} changed type: "
            f"expected {expected_type!r}, got {actual[field_name]!r}"
        )


@pytest.mark.parametrize("model", list(FIXTURE_FILES))
def test_golden_fixture_validates(model: type[BaseModel]) -> None:
    """The golden fixture must always parse into its contract without error."""
    fixture_path = FIXTURES_DIR / FIXTURE_FILES[model]
    data = json.loads(fixture_path.read_text())

    instance = model.model_validate(data)

    assert model.model_validate(json.loads(instance.model_dump_json())) == instance


def test_retired_contracts_are_gone() -> None:
    """SessionBundle/DiarizedTranscript were retired by P2.2 (TARGET_DESIGN P2.2):
    the phone-capture pipeline no longer exists; Event/Transcript replace them."""
    import importlib

    from src import contracts

    for name in ("SessionBundle", "DiarizedTranscript", "Utterance"):
        assert not hasattr(contracts, name)
    for mod in ("session_bundle", "diarized_transcript"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"src.contracts.{mod}")


# MED-4: golden fixtures for the Event variants (lane/tombstone semantics).
EVENT_VARIANTS = [
    ("event_voice.json", "trusted", False),
    ("event_recorder.json", "untrusted", False),
    ("event_transcript_tombstone.json", "untrusted", True),
]


@pytest.mark.parametrize(("fname", "lane", "tombstone"), EVENT_VARIANTS)
def test_event_variant_fixtures(fname: str, lane: str, tombstone: bool) -> None:
    data = json.loads((FIXTURES_DIR / fname).read_text())
    ev = Event.model_validate(data)
    assert Event.model_validate(json.loads(ev.model_dump_json())) == ev
    assert ev.lane == lane
    assert (ev.body is None and "rejected" in ev.meta) == tombstone


def test_all_event_kinds_have_a_fixture() -> None:
    kinds = {
        Event.model_validate(json.loads((FIXTURES_DIR / f).read_text())).kind
        for f in ("event.json", *(v[0] for v in EVENT_VARIANTS))
    }
    assert {"text", "voice", "recorder"} <= {k.value for k in kinds}
