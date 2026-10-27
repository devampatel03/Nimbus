# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Quarantine output schemas (ADR-0010, TARGET_DESIGN sections 4.3/4.4/5).

The quarantine model's answer is untrusted. It is accepted only if it parses
into one of the REGISTERED schemas below, every one of which is:

* strict (no type coercion), frozen, `extra="forbid"`;
* fully capped: every string has `max_length`, every list `max_length`,
  every int a lower and upper bound; no floats, dicts, nested models or `Any`.
  `QuarantineSchema.__pydantic_init_subclass__` enforces this at class
  creation, so an uncapped schema cannot even be defined;
* categorical where a field is categorical (StrEnum, bool, bounded int,
  datetime). Only those are readable by code (`Quarantined.fact`); every string
  is free text and reaches the trusted lane only inside a DataBlock.

Free text is NFKC-folded, control / zero-width / bidi characters are dropped,
whitespace is collapsed to single spaces (one line per field, so a value cannot
fake a "key: value" line), markdown images become a fixed token, and any
DataBlock delimiter or nonce pattern is REJECTED. Length caps apply after folding (U+FDFA expands ~18x under NFKC).

H18 interim default (ADR-0010 Proposed): every untrusted field goes through
the quarantine LLM. There is no deterministic short-field schema for external
content here; `LocalTextFields` covers Nimbus-local text only (tool output from
local tools, trusted memory), which is not externally authored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType, NoneType, UnionType
from typing import Annotated, Any, ClassVar, Final, Union, get_args, get_origin

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from src.lanes.data import SourceKind, fold

_DELIMITER_PATTERN = re.compile(r"[<>]{3,}|(?i:\bnonce\s*=)")
_WS = re.compile(r"\s+")
# Markdown image syntax is an exfil primitive (a client that renders it fetches
# the URL). Free text never needs it; it becomes a fixed token.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)?")
IMAGE_REMOVED = "[image removed]"


class UncappedSchemaError(TypeError):
    """A quarantine schema declared a field without a size/shape bound."""


def _clean_free_text(limit: int) -> Any:
    def clean(value: Any) -> Any:
        if not isinstance(value, str):
            return value  # strict str validation rejects it next
        if len(value) > 4 * limit + 64:
            raise ValueError("free text too long")  # before folding a huge value
        text = _MD_IMAGE.sub(IMAGE_REMOVED, _WS.sub(" ", fold(value)).strip())
        if _DELIMITER_PATTERN.search(text):
            raise ValueError("free text contains a delimiter pattern")
        return text

    return clean


def free_text(limit: int) -> Any:
    """A capped, normalised, single-line free-text string type."""
    if not 0 < limit <= 1000:
        raise ValueError("free text limit must be 1..1000")
    return Annotated[str, Field(max_length=limit), BeforeValidator(_clean_free_text(limit))]


def bounded_list(item: Any, max_items: int) -> Any:
    return Annotated[list[item], Field(max_length=max_items)]


def _json_violations(node: Mapping[str, Any], path: str, defs: Mapping[str, Any]) -> list[str]:
    if "$ref" in node:
        ref = str(node["$ref"]).rsplit("/", 1)[-1]
        target = defs.get(ref, {})
        if target.get("type") == "object" or "properties" in target:
            return [f"{path}: nested model"]
        return _json_violations(target, path, defs)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in node:
            out: list[str] = []
            for i, sub in enumerate(node[key]):
                out += _json_violations(sub, f"{path}|{i}", defs)
            return out
    if "enum" in node or "const" in node:
        return []
    kind = node.get("type")
    if kind == "string":
        if node.get("format") == "date-time":
            return []
        return [] if "maxLength" in node else [f"{path}: string without max_length"]
    if kind == "array":
        out = [] if "maxItems" in node else [f"{path}: list without max_length"]
        return out + _json_violations(node.get("items", {}), f"{path}[]", defs)
    if kind == "integer":
        ok = ("minimum" in node or "exclusiveMinimum" in node) and (
            "maximum" in node or "exclusiveMaximum" in node
        )
        return [] if ok else [f"{path}: unbounded integer"]
    if kind in ("boolean", "null"):
        return []
    return [f"{path}: disallowed type {kind or 'any'}"]


def schema_violations(schema: type[BaseModel]) -> list[str]:
    """Every field must be capped (see module doc). Empty list = compliant."""
    problems: list[str] = []
    cfg = schema.model_config
    if (
        cfg.get("extra") != "forbid"
        or cfg.get("frozen") is not True
        or cfg.get("strict") is not True
    ):
        problems.append("config must be strict, frozen, extra=forbid")
    try:
        js = schema.model_json_schema()
    except Exception:  # noqa: BLE001 - an unrenderable field is a violation
        return [*problems, "schema cannot be rendered"]
    defs = js.get("$defs", {})
    for name, prop in js.get("properties", {}).items():
        problems += _json_violations(prop, name, defs)
    return problems


def _is_free_text(annotation: Any) -> bool:
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin in (Union, UnionType, list, Annotated):
        return any(_is_free_text(a) for a in get_args(annotation) if a is not NoneType)
    return False


class QuarantineSchema(BaseModel):
    """Base of every quarantine output schema. Subclasses are checked at
    class creation (`UncappedSchemaError`)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    _categorical: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        problems = schema_violations(cls)
        if problems:
            raise UncappedSchemaError(f"{cls.__name__}: " + "; ".join(problems))
        cls._categorical = frozenset(
            n for n, f in cls.model_fields.items() if not _is_free_text(f.annotation)
        )

    @classmethod
    def categorical_fields(cls) -> frozenset[str]:
        return cls._categorical


# ------------------------------------------------------------------ the schemas


# Static aliases (mypy needs type expressions, not calls). `free_text(n)` builds
# the same shape dynamically for tests/new schemas.
def _ft(limit: int) -> BeforeValidator:
    return BeforeValidator(_clean_free_text(limit))


Name = Annotated[str, Field(max_length=120), _ft(120)]
Short = Annotated[str, Field(max_length=200), _ft(200)]
Address = Annotated[str, Field(max_length=254), _ft(254)]
Text400 = Annotated[str, Field(max_length=400), _ft(400)]
Text600 = Annotated[str, Field(max_length=600), _ft(600)]
Text800 = Annotated[str, Field(max_length=800), _ft(800)]


class EmailCategory(StrEnum):
    PERSONAL = "personal"
    WORK = "work"
    TRANSACTIONAL = "transactional"
    NEWSLETTER = "newsletter"
    NOTIFICATION = "notification"
    OTHER = "other"


class EmailDigest(QuarantineSchema):
    """One email message (headers and body), TARGET_DESIGN 4.4. Sender name and
    address are sender-controlled (spoofable), so they are free text too."""

    sender_name: Name = ""
    sender_address: Address = ""
    subject: Short = ""
    summary: Text600 = ""
    category: EmailCategory = EmailCategory.OTHER
    action_items: Annotated[list[Short], Field(max_length=5)] = []
    asks_for_reply: bool = False


class InviteKind(StrEnum):
    MEETING = "meeting"
    CALL = "call"
    EVENT = "event"
    REMINDER = "reminder"
    OTHER = "other"


class CalendarInviteDigest(QuarantineSchema):
    """A calendar invite or event. Times are data (an approval card shows the
    resolved payload before any calendar write, TARGET_DESIGN 4.5)."""

    title: Short = ""
    kind: InviteKind = InviteKind.OTHER
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    location: Short = ""
    organizer: Name = ""
    attendees: Annotated[list[Name], Field(max_length=20)] = []
    summary: Text400 = ""
    response_requested: bool = False


class DocType(StrEnum):
    ARTICLE = "article"
    NOTES = "notes"
    FORM = "form"
    INVOICE = "invoice"
    LETTER = "letter"
    WEB_PAGE = "web_page"
    OTHER = "other"


class DocumentDigest(QuarantineSchema):
    """A document, Drive/Notion page or web page."""

    title: Short = ""
    doc_type: DocType = DocType.OTHER
    summary: Text800 = ""
    key_points: Annotated[list[Short], Field(max_length=8)] = []


class TranscriptDigest(QuarantineSchema):
    """Recorded third-party speech (Recorder `.txt`), TARGET_DESIGN 4.3:
    summary <= 600 chars, candidate commitments <= 10 x 200, decisions."""

    summary: Text600 = ""
    commitments: Annotated[list[Short], Field(max_length=10)] = []
    decisions: Annotated[list[Short], Field(max_length=5)] = []
    speaker_count: Annotated[int, Field(ge=0, le=50)] | None = None


class NoteDigest(QuarantineSchema):
    """A forwarded message or a recalled untrusted memory item."""

    summary: Text600 = ""
    key_points: Annotated[list[Short], Field(max_length=5)] = []


REGISTERED_SCHEMAS: Final[frozenset[type[QuarantineSchema]]] = frozenset(
    {EmailDigest, CalendarInviteDigest, DocumentDigest, TranscriptDigest, NoteDigest}
)

SCHEMA_FOR_SOURCE: Final[Mapping[SourceKind, type[QuarantineSchema]]] = MappingProxyType(
    {
        SourceKind.EMAIL: EmailDigest,
        SourceKind.CALENDAR_INVITE: CalendarInviteDigest,
        SourceKind.DOCUMENT: DocumentDigest,
        SourceKind.WEB_PAGE: DocumentDigest,
        SourceKind.RECORDED_SPEECH: TranscriptDigest,
        SourceKind.FORWARDED_MESSAGE: NoteDigest,
        SourceKind.UNTRUSTED_MEMORY: NoteDigest,
    }
)


__all__ = [
    "REGISTERED_SCHEMAS",
    "SCHEMA_FOR_SOURCE",
    "CalendarInviteDigest",
    "DocType",
    "DocumentDigest",
    "EmailCategory",
    "EmailDigest",
    "InviteKind",
    "NoteDigest",
    "QuarantineSchema",
    "TranscriptDigest",
    "UncappedSchemaError",
    "bounded_list",
    "free_text",
    "schema_violations",
]
