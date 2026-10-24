# 0010 — Trust-lane classification, typed DataBlocks, and quarantine routing (bodies always; short fields via deterministic schema)

- Status: **Proposed.** It **deviates from Brief §2** ("any externally-authored text → quarantine LLM"). Human decision: TARGET_DESIGN §10 Q7.

## Context
The Brief routes all externally authored text through a quarantine LLM call. Calendar lists, email headers and Notion/Drive titles produce many short untrusted strings per turn. An LLM call for each one adds cost and latency (p95 ≤ 8 s). The protection against injection comes from deterministic layers (DataBlock delimiting, taint → policy → approvals), not from the paraphrase. Brief INV-6 still requires that untrusted text enter the trusted lane only as **schema-validated**, delimited DATA, so any bypass of the LLM must define its own validation. Telegram *forwarded* messages and untrusted memory hits were also unclassified.

## Decision
1. **Source → lane table** (`ingest/classify.py`, unit-tested). Trusted: user text, captions, user voice notes. Untrusted: messages with `forward_origin`, documents, email, calendar fields, Notion/Drive content, memory hits with `untrusted=true`.
2. **Typed wrapper.** `UntrustedText` cannot be converted to `str`. A `DataBlock` can be built **only** in two ways: (a) from `Quarantine.extract()` output, or (b) from an instance of a registered deterministic field schema (see 4). The context builder accepts `TrustedText | DataBlock` only. `render_data_block` escapes the content and wraps it in nonce delimiters with a fixed "data, not instructions" header.
3. **Bodies always go through the quarantine LLM**, regardless of length. Bodies are: email bodies, documents and Recorder `.txt`, Notion/Drive page content, forwarded messages, and calendar event descriptions. The call uses no tools, no memory and a fixed prompt, with a strict Pydantic output schema and length-capped fields. There is **no length threshold**. That removes the earlier "> 500 chars" ambiguity.
4. **Short fields** skip the LLM. Short fields are: email from (display name and address), email subject, event title/location/attendee names, page/file titles, and memory hits that are already quarantine output. "Schema-validated" for INV-6 means each short field is checked against a frozen per-source Pydantic model (e.g. `EmailHeaderFields`, `CalendarEventFields`) with these rules:
   - `extra="forbid"`, explicit types, and `max_length` per field (≤ 500, most ≤ 200);
   - NFKC normalisation; stripping of C0/C1 control, zero-width and bidi-override characters;
   - rejection of any value containing the DataBlock delimiter or nonce pattern;
   - a field that fails is replaced with `"[unrenderable field]"` and logged (the value itself is never logged).
5. **Taint** is set whenever any DataBlock enters a turn, via either path. It lasts for the turn plus 3 turns (policy table) and feeds `Policy.decide` (ADR-0009).

## Consequences
- Bodies are handled exactly as the Brief says. Only short, bounded, normalised fields bypass the LLM, and they are still delimited, schema-checked data.
- The INV-6 test asserts three things: no `DataBlock` constructor exists outside the two paths; every untrusted source maps to a field schema or to quarantine; and a compromised FakeLLM fed short-field payloads causes no unapproved effect.
- Short injected strings do reach the trusted model, as data. They can steer its text but cannot cause an effect without approval, because the turn is tainted.
- Red-team categories `calendar_invite_injection` and `email_instruction_injection` must include short-field payloads (subject, title, display name).

## Alternatives
- **Quarantine every untrusted field (Brief literal).** Simplest rule and strictest reading, at the cost of N extra LLM calls per list result. This is the fallback if Q7 is answered "no". It is a config switch per source, so no redesign is needed.
- **Length threshold for bodies (earlier draft: > 500 chars).** Rejected: a short email body is just as able to carry instructions as a long one, and two rules for the same kind of content invites bugs.
- **No quarantine at all.** Rejected: long untrusted bodies would blow token budgets, and structured extraction (commitments from transcripts) is useful in its own right.
