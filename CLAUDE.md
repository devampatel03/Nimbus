# Nimbus — CLAUDE.md

## What is this project

Nimbus is a private, self-hosted personal AI assistant. Full specs are in `docs/`.
Read `docs/AGENT_OPERATING_MANUAL.md` before doing any work.

## Architecture (quick reference)

- **FastAPI** backend in `src/api/`
- **LangGraph** agent runtime in `src/orchestrator/`
- **Memory**: Redis (working) + Qdrant (episodic) + Postgres+AGE (semantic + temporal graph) in `src/memory/`
- **Personality**: user traits + agent character in `src/personality/`
- **Perception**: ASR + diarization in `src/perception/`
- **Security**: scrubber + key hierarchy + erasure in `src/security/`
- **Tools**: calendar, gmail, tasks, etc. in `src/tools/`
- **Workers**: async extraction jobs in `src/workers/`
- **Tests**: `tests/unit/`, `tests/integration/`, `tests/eval/`, `tests/adversarial/`

## Invariants — these are tested in CI, never violate them

1. **INV-1**: Exactly one function performs cloud model egress (`src/security/scrubber.py:execute_external`)
2. **INV-2**: No memory/transcript/reasoning in any egress payload
3. **INV-3**: Graph facts never mutated in place — supersession only
4. **INV-4**: Mode A response path never blocks on a network call
5. **INV-5**: Every derived memory row has non-null provenance

## Commands

- `uv run pytest` — run tests
- `uv run ruff check src/` — lint
- `uv run mypy src/` — type check
- `docker compose up -d` — start postgres, redis, qdrant

## Current phase

**Phase 0 — Foundation & de-risking**
See `docs/EXECUTION_PLAN.md` for task list.

## Task tracking

| Task ID | Status | Notes |
|---------|--------|-------|
| T0.1    | done | CI, pre-commit (ruff/mypy/detect-secrets/license header), secret-scan verified blocking; 5 invariant stubs added (fail correctly, no src/ yet) |
| T0.2    | done | Local dev infra: docker-compose (postgres+AGE+pgvector, redis, qdrant), config/settings.py (Pydantic BaseSettings), src/db/{postgres,redis,qdrant}.py async clients, src/api/health.py GET /health, tests/unit/test_health.py (2 passing). Note: this is the "local dev infra" work as user-directed; docs/EXECUTION_PLAN.md's actual T0.2 (P-SEC: LUKS/WireGuard/egress proxy) was not done — label mismatch flagged to and confirmed by user. |
| T0.8    | done | All 6 frozen interface contracts (SessionBundle, DiarizedTranscript, MemoryQuery/Result, UserProfileSlice, ToolIntent/Result, ProactiveCandidate) added as Pydantic models in `src/contracts/`, each with a golden fixture in `tests/fixtures/` and a field/type compatibility test in `tests/unit/test_contracts.py` (16 tests passing; verified the compatibility test fails on a deliberate field removal). |
| T0.9    | partial | `NOTICE` file created at repo root covering all `pyproject.toml` deps (licences confirmed via installed package metadata, not memory) plus model-weight attributions from PRD section 10.1, with correct CC-BY-4.0 attribution for Parakeet-TDT 0.6B v2. Scope note: EXECUTION_PLAN.md's T0.9 acceptance criterion also requires "SBOM generated in CI" — that automation was not built; only the manually-curated NOTICE file was in scope of this session's instruction. Flagging per the "spec is right, raise it" rule — CI SBOM generation remains outstanding. |

## Rules for agents

1. Read `docs/AGENT_OPERATING_MANUAL.md` section 2 (universal checklist) before completing ANY task
2. Update this file's task tracking table after completing work
3. Update MEMORY.md with architectural decisions made
4. Never modify files in `tests/eval/` without explicit permission
5. Every commit message: `[T{id}] {description} — satisfies {REQ-IDs}`
6. Run `uv run pytest && uv run ruff check src/ && uv run mypy src/` before declaring done
7. Use ECC skills: invoke `python-patterns` for Python code, `python-testing` for tests, `postgres-patterns` for DB work, `security-review` for anything touching auth/crypto/network
8. After significant code changes, let Graphify rebuild the knowledge graph