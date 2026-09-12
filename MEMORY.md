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