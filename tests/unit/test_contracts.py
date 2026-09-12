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
    DiarizedTranscript,
    MemoryQuery,
    MemoryResult,
    ProactiveCandidate,
    SessionBundle,
    ToolIntent,
    ToolResult,
    UserProfileSlice,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# model -> fixture file -> {field_name: annotation!r}
# Recorded once at freeze time; a field removal or type change breaks the
# corresponding assertion below.
EXPECTED_FIELDS: dict[type[BaseModel], dict[str, str]] = {
    SessionBundle: {
        "session_id": "UUID",
        "user_id": "UUID",
        "mode": "Literal['mode_a', 'mode_b']",
        "started_at": "datetime",
        "ended_at": "datetime | None",
        "audio_ref": "str",
        "sample_rate_hz": "int",
        "codec": "str",
        "language_hint": "str | None",
        "device_id": "str",
        "client_app_version": "str",
    },
    DiarizedTranscript: {
        "session_id": "UUID",
        "utterances": "list[Utterance]",
        "source_asr_model": "str",
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
    SessionBundle: "session_bundle.json",
    DiarizedTranscript: "diarized_transcript.json",
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
