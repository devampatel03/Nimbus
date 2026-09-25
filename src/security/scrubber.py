# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The scrubber: the sole cloud-egress boundary (docs/TECHNICAL_ARCHITECTURE.md
section 8; INV-1, INV-2; PRD SCR-1 through SCR-4, SCR-6, SCR-7).

`execute_external` is the ONLY function in this codebase that reaches a
cloud model. It must not accept transcripts, memory rows, graph edges,
personality traits, stance entries, reasoning chains, raw user
utterances, or person names — only a `ToolIntent`, whose schema is a
closed whitelist (`extra="forbid"`, primitives-only params).

Pipeline (8.2): kill switch -> schema/registry validation -> entity
substitution -> PII hard-fail -> audit log (before send) -> egress ->
response -> ref resolution -> audit log (after response).

AD-12 (this session): `execute_external` also takes a `session_id: str`
argument, which is not part of `ToolIntent`. This scopes the per-session
entity-ref store (SCR-3: "resolved after response returns") and is a
local routing parameter, not payload content — it carries no memory,
transcript, or reasoning, so it does not weaken INV-2. Section 8.1's
`execute_external(intent: ToolIntent) -> ToolResult` snippet is
abbreviated pseudocode for the payload contract, not a literal function
signature freeze (unlike the six contracts frozen in `src/contracts/`).

SCR-5 (network-layer egress allowlist, independent of the app / T3.10)
is explicitly out of scope for this session's instruction and is not
implemented here — flagged per the "spec is right, raise it" rule. The
only network-layer control in this module is the https-only URL check
below, which is app-layer, not a substitute for T3.10.

No internal retry loop is implemented by design (8.4: "retry logic
bypassing the scrubber on failure" is a named failure mode). A caller
that needs to retry a failed egress must re-invoke `execute_external` in
full, so every attempt is re-validated, re-substituted, and re-audited —
never retried at the transport layer alone.

This session: `execute_external` makes a real call to the Gemini API
(`generateContent`, `responseMimeType: application/json`, forced to a
single JSON object so the response is parseable without a second model
round trip) instead of the previous generic-stub POST. `payload` (the
scrubbed `ToolIntent`, already validated/substituted/PII-checked above)
is sent as the sole prompt content -- no additional free text is added,
so INV-2 stays satisfied by construction. Token counts from Gemini's
`usageMetadata` are logged (not audited to disk) per this session's
"monitor Gemini response costs" instruction.

Scope note: the public Gemini API has no general-purpose "execute this
write action against the caller's Google Calendar/Gmail/Drive" capability
-- section 8.3's "the cloud model does execution" is this project's
design intent, not a capability the real API grants on its own. What is
real here: a genuine network call to Gemini, real token cost, and real
response parsing/error handling. What is not yet real, and is not
claimed to be: an actual mutation landing in a live Google account.
Calendar/Gmail/Drive integrations (this session) stay intent-builders
only, per that scope boundary -- see their own module docstrings.
"""

import json
import logging
import secrets
import threading
from typing import Any

import httpx

from config.settings import settings
from src.contracts.tool_intent import Primitive, ToolIntent, ToolResult
from src.security import audit_log, kill_switch, pii_detector

MAX_PARAM_STRING_LENGTH = 500  # 8.4: caps free-text params carrying context

_logger = logging.getLogger(__name__)

# Closed whitelist: tool -> allowed actions. Mirrors src/tools/registry.py's
# real ToolRegistry (T4.1) -- kept as an explicit, separate list rather than
# introspecting the registry directly, per BaseTool's own docstring note
# that the two are "not yet unified"; this session's task (item 6) is to
# keep the two in sync by hand, not to unify them structurally.
# `tasks`/`reminders` are purely local tools that never call
# execute_external (see their own modules) -- listed here anyway so this
# stays the single place that documents the full registry surface.
ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    "calendar": frozenset({"create", "update", "delete", "list"}),
    "gmail": frozenset({"send", "search", "read", "draft"}),
    "tasks": frozenset({"create", "update", "complete", "delete", "list", "get"}),
    "drive": frozenset({"list", "search", "read"}),
    "reminders": frozenset({"create", "update", "delete", "list", "get"}),
}

_REF_TYPES = frozenset({"person", "org", "location"})


class SchemaValidationError(ValueError):
    """Raised when a ToolIntent fails registry/whitelist/param validation."""


class PIIDetectedError(RuntimeError):
    """Raised when the PII detector finds anything in the outbound payload."""


class ExternalCallError(RuntimeError):
    """Raised when the cloud call itself fails. Message is sanitised."""


class _SessionRefStore:
    """Per-session, in-memory, bidirectional name<->opaque-ref mapping.

    Refs are non-sequential (secrets.token_hex) per 8.4's "ref collision
    or leakage of the ref mapping" guard, and this store is never
    transmitted — only its ref tokens are.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._name_to_ref: dict[str, str] = {}
        self._ref_to_name: dict[str, str] = {}

    def ref_for(self, entity_type: str, canonical_name: str) -> str:
        if entity_type not in _REF_TYPES:
            raise ValueError(f"unknown entity_type: {entity_type!r}")
        with self._lock:
            existing = self._name_to_ref.get(canonical_name)
            if existing is not None:
                return existing
            ref = f"<{entity_type}_{secrets.token_hex(4)}>"
            self._name_to_ref[canonical_name] = ref
            self._ref_to_name[ref] = canonical_name
            return ref

    def resolve(self, ref: str) -> str | None:
        with self._lock:
            return self._ref_to_name.get(ref)

    def known_names(self) -> dict[str, str]:
        with self._lock:
            return dict(self._name_to_ref)

    def ref_to_name_snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._ref_to_name)


_stores_lock = threading.Lock()
_session_stores: dict[str, _SessionRefStore] = {}


def _get_store(session_id: str) -> _SessionRefStore:
    with _stores_lock:
        store = _session_stores.get(session_id)
        if store is None:
            store = _SessionRefStore()
            _session_stores[session_id] = store
        return store


def create_ref(session_id: str, entity_type: str, canonical_name: str) -> str:
    """Register `canonical_name` for this session, returning its opaque ref.

    Idempotent: registering the same name twice in a session returns the
    same ref. Called by the orchestrator/local model before constructing
    a `ToolIntent`, so `entity_refs`/`params` only ever carry ref tokens.
    """
    return _get_store(session_id).ref_for(entity_type, canonical_name)


def resolve_ref(session_id: str, ref: str) -> str | None:
    """Resolve one opaque ref back to its canonical name for this session."""
    return _get_store(session_id).resolve(ref)


def _substitute_known_entities(
    session_id: str, params: dict[str, Primitive]
) -> dict[str, Primitive]:
    """Defense-in-depth: if a name already registered in this session's ref
    store appears verbatim in a param string, replace it with its ref.
    Names never registered here still get caught by the PII detector.
    """
    names = _get_store(session_id).known_names()
    substituted: dict[str, Primitive] = {}
    for key, value in params.items():
        if isinstance(value, str):
            for name, ref in names.items():
                value = value.replace(name, ref)
        substituted[key] = value
    return substituted


def _resolve_refs_in_value(session_id: str, value: Primitive) -> Primitive:
    if not isinstance(value, str):
        return value
    ref_to_name = _get_store(session_id).ref_to_name_snapshot()
    resolved = value
    for ref, name in ref_to_name.items():
        if ref in resolved:
            resolved = resolved.replace(ref, name)
    return resolved


def _validate_intent(intent: ToolIntent) -> None:
    allowed_actions = ALLOWED_TOOLS.get(intent.tool)
    if allowed_actions is None:
        raise SchemaValidationError(f"unknown tool: {intent.tool!r}")
    if intent.action not in allowed_actions:
        raise SchemaValidationError(f"unknown action {intent.action!r} for tool {intent.tool!r}")
    for key, value in intent.params.items():
        if isinstance(value, str) and len(value) > MAX_PARAM_STRING_LENGTH:
            raise SchemaValidationError(
                f"param {key!r} exceeds max length ({len(value)} > {MAX_PARAM_STRING_LENGTH})"
            )


async def execute_external(intent: ToolIntent, session_id: str) -> ToolResult:
    """The ONLY function in this codebase that reaches a cloud model. INV-1."""
    if kill_switch.is_engaged():
        raise kill_switch.KillSwitchEngagedError("cloud egress is disabled (kill switch engaged)")

    _validate_intent(intent)

    scrubbed_params = _substitute_known_entities(session_id, intent.params)
    scrubbed_intent = intent.model_copy(update={"params": scrubbed_params})
    payload: dict[str, Any] = scrubbed_intent.model_dump(mode="json")

    findings = pii_detector.scan_payload(payload)
    if findings:
        categories = tuple(sorted({f.category for f in findings}))
        audit_log.write_entry(
            audit_log.AuditLogEntry(
                session_id=session_id,
                tool=intent.tool,
                action=intent.action,
                status="blocked_pii",
                blocked_categories=categories,
            )
        )
        raise PIIDetectedError(
            f"blocked: PII detected in outbound payload ({', '.join(categories)})"
        )

    audit_log.write_entry(
        audit_log.AuditLogEntry(
            session_id=session_id,
            tool=intent.tool,
            action=intent.action,
            status="sent",
            payload=payload,
        )
    )

    if not settings.cloud_egress_url.startswith("https://"):
        raise ExternalCallError("cloud egress URL must use https")

    gemini_url = f"{settings.cloud_egress_url}/{settings.gemini_model}:generateContent"
    gemini_request = {
        "contents": [{"role": "user", "parts": [{"text": json.dumps(payload)}]}],
        # Forces a single JSON object back, so the response is parseable
        # without a second model round trip (see module docstring).
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }

    try:
        async with httpx.AsyncClient(timeout=settings.cloud_egress_timeout_seconds) as client:
            response = await client.post(
                gemini_url,
                json=gemini_request,
                headers={
                    "x-goog-api-key": settings.cloud_egress_api_key,
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            gemini_response = response.json()
    except httpx.HTTPError as exc:
        audit_log.write_entry(
            audit_log.AuditLogEntry(
                session_id=session_id,
                tool=intent.tool,
                action=intent.action,
                status="error",
                response_summary=type(exc).__name__,  # sanitised: no payload/body echoed
            )
        )
        raise ExternalCallError(f"cloud egress failed: {type(exc).__name__}") from None

    usage = gemini_response.get("usageMetadata", {})
    _logger.info(
        "gemini tool-execution call: tool=%s action=%s prompt_tokens=%s "
        "response_tokens=%s total_tokens=%s",
        intent.tool,
        intent.action,
        usage.get("promptTokenCount"),
        usage.get("candidatesTokenCount"),
        usage.get("totalTokenCount"),
    )

    result_data = _parse_gemini_result(gemini_response, session_id=session_id, intent=intent)

    resolved_result: dict[str, Primitive] = {
        k: _resolve_refs_in_value(session_id, v) for k, v in result_data.items()
    }

    audit_log.write_entry(
        audit_log.AuditLogEntry(
            session_id=session_id,
            tool=intent.tool,
            action=intent.action,
            status="response_received",
            response_summary=f"{len(resolved_result)} field(s) returned",
        )
    )

    return ToolResult(
        tool=intent.tool,
        action=intent.action,
        status="success",
        result=resolved_result,
        entity_refs=intent.entity_refs,
    )


def _audit_parse_failure(session_id: str, intent: ToolIntent, reason: str) -> None:
    audit_log.write_entry(
        audit_log.AuditLogEntry(
            session_id=session_id,
            tool=intent.tool,
            action=intent.action,
            status="error",
            response_summary=reason,  # sanitised: reason only, never the raw body
        )
    )


def _parse_gemini_result(
    gemini_response: dict[str, Any], *, session_id: str, intent: ToolIntent
) -> dict[str, Primitive]:
    """Extract and validate the JSON object Gemini returned in its single
    candidate's text part. Any parse/shape failure is sanitised (8.4:
    never echo the raw response body back out through an exception or
    the audit log) before being raised.
    """
    candidates = gemini_response.get("candidates") or []
    if not candidates:
        _audit_parse_failure(session_id, intent, "no candidates returned")
        raise ExternalCallError("cloud egress failed: no candidates returned")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))

    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        _audit_parse_failure(session_id, intent, "non-JSON response body")
        raise ExternalCallError("cloud egress failed: non-JSON response") from None

    if not isinstance(parsed, dict):
        _audit_parse_failure(session_id, intent, "response was not a JSON object")
        raise ExternalCallError("cloud egress failed: response was not a JSON object")

    result: dict[str, Primitive] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, Primitive):
            _audit_parse_failure(session_id, intent, "response value was not a primitive")
            raise ExternalCallError("cloud egress failed: response value was not a primitive")
        result[key] = value
    return result
