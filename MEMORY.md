# Nimbus — MEMORY.md

## Architectural decisions

| ID | Decision | Rationale | Date | Alternatives considered |
|----|----------|-----------|------|------------------------|
| AD-1 | Postgres + Apache AGE for bi-temporal graph | Single DB to encrypt/backup; AGE gives graph queries without Neo4j ops burden | 2026-09-12 | Neo4j (ops), plain Postgres (no graph queries) |
| AD-2 | Gemini 3.8 Flash for tool execution only | $0.75/M input, strong tool calling, but 13.3s TTFT disqualifies it from Mode A | 2026-09-12 | Local-only (weaker tool calling), OpenAI (more expensive) |
| AD-3 | Scrubber as sole egress point | Privacy boundary must be architectural, not policy | 2026-09-12 | Per-tool sanitisation (too many surfaces) |
| AD-4 | Phone-first, no wearable hardware | Removes ~4 months from critical path; phone mic is sufficient for Phase 1-6 | 2026-09-12 | Omi fork (deferred) |

## Interface contracts

| Contract | Schema location | Version | Last updated |
|----------|----------------|---------|--------------|
| (to be filled as contracts are defined) |

## Known issues and tech debt

| Issue | Severity | Filed by | Phase |
|-------|----------|----------|-------|
| (none yet) |

## What the agent learned this session

(Agents: append here after every session. Format: date, what you learned, what you'd do differently.)

- 2026-09-12 (T0.1): Pre-commit uses ruff, mypy (strict, `src/` only — mypy errors if pointed at an empty dir), detect-secrets (baseline at `.secrets.baseline`), and `Lucas-C/pre-commit-hooks:insert-license` for `.py` files (header text in `config/license/header.txt`). Verified end-to-end: committing a file containing `sk-abc123fake` is blocked by the detect-secrets hook (exit 1) before it reaches git history.
- The 5 invariant test stubs in `tests/eval/invariants/` (INV-1..5) each import a not-yet-existing `src/...` symbol and fail at collection (`ModuleNotFoundError`) — this is the correct/expected state until the owning pods (P-SEC, P-MEMORY, P-AGENT) implement the real modules. Do not stub out fake `src/` code to make these pass.
- Process note: AGENT_OPERATING_MANUAL assigns invariant tests to P-EVAL (T0.10), not P-SEC (T0.1). This session's task explicitly instructed creating the 5 stubs as part of T0.1 anyway — flagging per the manual's "raise it, don't silently diverge" rule rather than editing the plan myself. T0.10 in EXECUTION_PLAN.md may now be redundant/needs reconciling with what already exists.
- `.claude/hooks/enforce-boundary.sh` previously had a path-comparison bug (POSIX `/c/...` ROOT vs Windows-style `C:/...` resolved paths never matched) that blocked all in-repo Read/Write/Edit; the user fixed it mid-session by normalizing both sides to lowercase Windows-style paths.