# 0005 — Break the tools <-> `execute_external` cycle by deleting the intent-builder tools; tools never call an LLM

- Status: Accepted (P0.4, 2026-10-24). Supersedes MEMORY.md AD-14, AD-25, AD-26.

## Context
COUPLING §3 flags a cycle. `calendar.py`, `gmail.py` and `drive.py` import `scrubber.execute_external`, so the `llm` consolidation (R9) cannot delete it until the tools stop calling it. But the tools cannot get real clients until the egress allowlist exists. The proposed fix was a shim behind `LEGACY_EGRESS=1`.

The three tools have no real capability today. They build intents for a Gemini call that cannot perform Workspace actions (AD-25, MEMORY.md tech debt). Gmail also carries a `send` action that R6 forbids.

## Decision
- Order: `net` (P1.4a), then `llm` (P1.4b), then **one PR (P1.4c)** that deletes `execute_external`, `ALLOWED_TOOLS`, the three intent-builder tools (including Gmail `send`) and their tests. No shim.
- Calendar, Gmail (`gmail_draft`) and Drive (`drive_read`) are rebuilt in W3 on the new `Tool` protocol over `src/integrations/**`.
- **Tools never call the LLM and never read memory.** They receive a `ToolContext` containing a service-scoped `net` client, an idempotency key and the clock. Content from outside that tools return is marked untrusted (`ToolOutput.untrusted`), and the orchestrator, not the tool, routes it through quarantine.

## Consequences
- The dependency graph stays acyclic: `tools -> integrations -> net`, `orchestrator -> {llm, policy, approvals, tools, memory}`.
- Between P1.4c and W3 the registry holds only `tasks` and `reminders`. Nothing real is lost.
- The `test_allowed_tools_matches_the_real_tool_registry` drift test is deleted. Drift becomes impossible because the registry is the single source.

## Alternatives
- **Deprecated shim (COUPLING option 1).** Rejected: keeps dead code alive to protect capability that does not exist.
- **Merge the llm and tools rewrites into one PR (COUPLING option 2).** Rejected: too large to review, and the real tools are blocked on OAuth (H4).
