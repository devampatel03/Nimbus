# 0010 — Trust-lane classification, typed DataBlocks, and quarantine only for long untrusted text

- Status: Accepted. **Deviates from Brief §2** ("any externally-authored text → quarantine LLM").

## Context
The Brief routes all externally authored text through a quarantine LLM call. Calendar lists, email headers and Notion titles produce many short untrusted strings per turn. An LLM call for each one adds cost and latency (p95 ≤ 8 s) without adding safety: the protection comes from the text arriving as **delimited data with taint set**, not from the paraphrase. Two sources were also left unclassified: Telegram *forwarded* messages, and memory retrieved from untrusted sources.

## Decision
1. **Source → lane table** (`ingest/classify.py`, unit-tested). Trusted: user text, captions, user voice notes. Untrusted: messages with `forward_origin`, documents (Recorder `.txt`), email, calendar free-text fields, Notion/Drive content, memory hits with `untrusted=true`.
2. **Typed wrapper.** `UntrustedText` cannot be converted to `str`. It reaches a prompt only through `render_data_block()`, which applies an escaped, nonce-delimited block with a fixed "data, not instructions" header. The context builder accepts only `TrustedText | DataBlock`. That is INV-6, enforced by type plus test.
3. **Quarantine LLM** (no tools, no memory, fixed prompt, strict schema with length-capped fields) is used when the untrusted text is longer than 500 chars or is a document/email body. Shorter fields are length-capped and go straight into a DataBlock.
4. **Taint** is set whenever any DataBlock enters a turn. It lasts for the turn plus 3 turns (policy-table value) and feeds `Policy.decide` (ADR-0009).

## Consequences
- Fewer LLM calls, and the safety argument rests on deterministic layers (taint, policy, approvals), consistent with the Brief's "model refusal never counts".
- Short injected strings do reach the trusted model as data. That model may be steered, but it cannot produce an effect without approval when tainted.
- The red-team categories `calendar_invite_injection` and `shared_doc_injection` must include short-field payloads.

## Alternatives
- **Quarantine everything (Brief literal).** Rejected on cost and latency. It can be enabled per source via config if the bake-off model handles DataBlocks poorly.
- **No quarantine at all.** Rejected: long untrusted bodies would blow token budgets, and structured extraction is useful in its own right (commitments from transcripts).
