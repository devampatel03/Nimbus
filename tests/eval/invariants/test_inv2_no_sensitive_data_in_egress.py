# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-2: No memory/transcript/reasoning content in any egress payload.

`ToolIntent` (src/contracts/tool_intent.py) is the only object the
scrubber's `execute_external` (INV-1) may accept, per section 8.1: no
transcripts, memory rows, graph edges, personality traits, stance entries,
reasoning chains, raw user utterances or person names may cross this
boundary — params are primitives only and entities are opaque local refs.

This is a schema-level check: field names must not smuggle memory/
transcript/reasoning content, `params`/`result` values must be primitives
(no nested dict/list that could carry an arbitrary blob), and the models
must reject unknown fields outright (`extra="forbid"`) so a caller cannot
attach a forbidden field ad hoc.
"""

from typing import get_args

from src.contracts.tool_intent import Primitive, ToolIntent, ToolResult

FORBIDDEN_SUBSTRINGS = (
    "transcript",
    "memory",
    "reasoning",
    "thought",
    "utterance",
    "raw_text",
    "chain_of_thought",
)


def _assert_no_forbidden_field_names(model_cls: type) -> None:
    for name in model_cls.model_fields:
        lowered = name.lower()
        hits = [bad for bad in FORBIDDEN_SUBSTRINGS if bad in lowered]
        assert not hits, (
            f"{model_cls.__name__}.{name} looks like it could carry "
            f"memory/transcript/reasoning content across the egress boundary: {hits}"
        )


def _assert_extra_forbidden(model_cls: type) -> None:
    assert model_cls.model_config.get("extra") == "forbid", (
        f"{model_cls.__name__} must set extra='forbid' so no ad hoc field "
        "can smuggle memory/transcript/reasoning content past the schema"
    )


def test_tool_intent_has_no_memory_transcript_or_reasoning_fields():
    _assert_no_forbidden_field_names(ToolIntent)


def test_tool_result_has_no_memory_transcript_or_reasoning_fields():
    _assert_no_forbidden_field_names(ToolResult)


def test_tool_intent_forbids_extra_fields():
    _assert_extra_forbidden(ToolIntent)


def test_tool_result_forbids_extra_fields():
    _assert_extra_forbidden(ToolResult)


def test_tool_intent_params_are_primitives_only():
    """`params` must be `dict[str, Primitive]` — no nested object/list that
    could carry an arbitrary memory/transcript/reasoning blob.
    """
    annotation = ToolIntent.model_fields["params"].annotation
    args = get_args(annotation)
    assert args, f"expected a parameterized dict annotation, got {annotation!r}"
    value_type = args[1]
    assert (
        value_type == Primitive
    ), f"ToolIntent.params values must be Primitive, got {value_type!r}"


def test_tool_intent_entity_refs_are_opaque_strings():
    """`entity_refs` must map to opaque string refs, never resolved names
    or nested structures (section 8.1: "entities are opaque local refs").
    """
    annotation = ToolIntent.model_fields["entity_refs"].annotation
    args = get_args(annotation)
    assert args == (str, str), f"ToolIntent.entity_refs must be dict[str, str], got {annotation!r}"


def test_tool_intent_cannot_construct_with_forbidden_field():
    import pydantic

    try:
        ToolIntent(
            tool="calendar",
            action="create",
            params={},
            entity_refs={},
            risk_class="read",
            transcript="leaked",  # type: ignore[call-arg]
        )
    except pydantic.ValidationError:
        return
    raise AssertionError("ToolIntent accepted an ad hoc 'transcript' field")
