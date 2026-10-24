# 0003 — The single `src/llm` module replaces `scrubber.execute_external`; entity substitution dropped

- Status: Accepted (P0.4, 2026-10-24). Supersedes MEMORY.md AD-2, AD-3, AD-12, AD-14, AD-24.

## Context
Brief INV-1/R4 require that every LLM call go through one module with an audit log, a scrubber, a spend cap and a kill switch. Today:
- `src/llm/client.py` is an OpenAI-SDK client (`LLMClient` Protocol) with none of those four controls.
- `src/security/scrubber.py:execute_external` holds the controls but sends a Gemini `generateContent` request on behalf of *tools*. That design was never real: MEMORY.md records that Gemini cannot execute Workspace actions.
- Entity substitution (`_SessionRefStore`) swaps names for opaque refs. Under the Brief's accepted stance (the provider sees plaintext), it gives no meaningful privacy, and it hurts reasoning ("remind me to call Mom" turns into `<person_3>`).

## Decision
`src/llm/` is the only package that may import an LLM SDK. All calls use one primitive, `LLMClient.chat(ChatRequest)`, plus `transcribe()`. The guard order is fixed:
1. Kill switch.
2. Lane check (QUARANTINE requests carry no tools and no memory).
3. Scrub.
4. Spend-cap pre-check.
5. Audit row.
6. Provider call through `net.Service.LLM`.
7. Audit completion.

Every failure raises `LLMRefused` with **no network I/O**.

How the existing pieces are handled:
- Move `pii_detector.py` to `llm/scrub.py`, retuned. Hard-block secrets, tokens and canaries. Redact card/Aadhaar/OTP-like numbers. Pass names and emails through.
- Move the kill switch and audit to Postgres (`system_flag`, `llm_audit`).
- Delete `execute_external`, `ALLOWED_TOOLS`, `_SessionRefStore` and their tests.
- `stream()` is removed: Telegram sends whole messages.
- `transcribe()` hides the provider difference: chat `input_audio` vs `/audio/transcriptions`. Support for ogg/opus is inconsistently documented as of 2026-10-24 (Gemini OpenAI-compat docs; OpenAI STT guide), so the bake-off verifies it. If the winning provider rejects ogg, the adapter transcodes with `ffmpeg` (added to the image only in that case).

## Consequences
- One audited path, and a spend cap that can actually be enforced, since cost is summed from `llm_audit`.
- The privacy claim narrows to what is true: secrets and canaries never leave, everything else may. This matches Brief §8.
- During M8 there is a deprecated `VLLMClient` alias, so `extractor.py`, `agent_character.py` and the orchestrator stay green until they migrate.

## Alternatives
- **Keep `execute_external` as a shim (COUPLING §3 option).** Rejected: no caller needs it once the intent-builder tools are deleted (ADR-0005).
- **Keep entity substitution.** Rejected for now. A TEE lane (Brief §6) is the real upgrade path. Substitution could come back later as an opt-in `scrub` mode.
- **LiteLLM as the adapter.** Rejected: a large dependency for one OpenAI-compatible endpoint, and it hides transport details from the R13 allowlist.
