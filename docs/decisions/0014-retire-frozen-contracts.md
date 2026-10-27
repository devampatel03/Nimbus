# 0014 — Retire the voice- and egress-shaped frozen contracts; keep the golden-fixture pattern

- Status: Accepted. Amends T0.8's "frozen interface contracts" (CLAUDE.md task table, MEMORY.md "Interface contracts").
- Executed: `SessionBundle` and `DiarizedTranscript` replaced by `Event` and `Transcript` (P2.2, PR #20). `ToolIntent` and `ToolResult` deleted (P2.5b, PR #25). Deviation: `ToolOutput` lives in `src/orchestrator/dispatch.py`, not `contracts/tool_io.py`. The stale `SessionBundle` docstring in `src/memory/schemas/session.py` is still open.

## Context
T0.8 froze six Pydantic contracts:
- `SessionBundle` and `DiarizedTranscript` are shaped for the retired diarization stack.
- `ToolIntent` and `ToolResult` are shaped for scrubber egress (primitives only, opaque entity refs). AD-18 shows `ToolResult.result: dict[str, Primitive]` already forcing workarounds.
- `MemoryQuery/Result`, `UserProfileSlice` and `ProactiveCandidate` are storage- and channel-agnostic.

## Decision
- **Keep:** `MemoryQuery/MemoryResult` (adding `as_of`, `include_untrusted`, `untrusted` on results), `UserProfileSlice`, `ProactiveCandidate`.
- **Replace:**
  - `SessionBundle` → `Event` (source_key, kind, lane, untrusted, received_at, payload refs).
  - `DiarizedTranscript` → `Transcript` (segments with an optional speaker).
  - `ToolIntent/ToolResult` → `ProposedCall` / `ToolOutput` (`tools/base.py`, `contracts/tool_io.py`), with typed `args_model` per action and `untrusted` output separated.
- Keep `RiskClass` (renamed `WRITE_INTERNAL`, ADR-0009) and `Primitive`.
- Every contract keeps a golden fixture in `tests/fixtures/` and a compatibility test in `tests/unit/test_contracts.py`. That pattern is sound and is retained.

## Consequences
- The extractor is retyped from `DiarizedTranscript` to `Transcript` (P2.2). The `session` table becomes `event`, and `source_session` FKs become `source_event_id` (P1.2c migration).
- Repo INV-2's test disappears with `ToolIntent` (ADR-0002).

## Alternatives
- **Keep `ToolIntent` as the tool-call envelope.** Rejected: its constraints exist to limit cloud egress of tool params. Under the new design tool args go to Google/Notion directly, and they need typed nested structures (attendee lists, time ranges).
