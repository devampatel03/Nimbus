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
| T0.10   | done | All 5 invariant stubs in `tests/eval/invariants/` made concrete: INV-1 (AST scan of `src/` for httpx/requests/aiohttp call sites, self-adjusting — 0 hits pre-scrubber, exactly 1 inside `execute_external` once it exists), INV-2 (schema checks on `ToolIntent`/`ToolResult`: no forbidden field-name substrings, `extra="forbid"`, `params` values are `Primitive`, ad hoc field raises `ValidationError`), INV-3 (AST scan of `src/memory/graph.py` for direct assignment or `update(GraphEdge).values()` touching `object_id`/`object_literal`/`valid_from`), INV-4 (`pytest.mark.skipif` stub — no `src/orchestrator/mode_a.py` yet), INV-5 (real-Postgres query for `NULL source_session` rows + IntegrityError tests on `GraphEdge`/`Commitment`/`Decision`). 38 passed, 1 skipped; verified INV-1 and INV-3 fail correctly when the underlying code is deliberately broken (see MEMORY.md). |
| T2.9    | done (schema + repo only; scope note below) | Bi-temporal graph schema per TECHNICAL_ARCHITECTURE.md 6.1: `src/memory/schemas/{base,graph,session,person,commitment,decision}.py` (SQLAlchemy 2.0 async ORM), `src/memory/graph.py` (`GraphRepository`: insert/supersede/find_contradictions), Alembic migration `0dc85954088a` applied and round-tripped (`upgrade head` / `downgrade base` / `upgrade head`) against real Postgres. `tests/integration/test_supersession.py` and `test_contradiction.py` pass against real Postgres. Scope note: `session`/`person`/`commitment`/`decision` are ahead of their assigned phases (T2.8, T4.8/T4.9, T5.2/T5.3) — built now per explicit instruction, flagged in each module's docstring; `graph_node`'s schema is not spec-mandated (minimal design, flagged for review). |
| T1.6    | done | Working + episodic memory tiers: `src/memory/manager.py` (`MemoryManager.write_episodic`/`write_semantic`/`query`), backed by real Qdrant (episodic vectors) and Postgres (`GraphRepository`, supersession — never UPDATE, INV-3-safe). `tests/integration/test_manager.py` (5 tests) writes and reads across episodic + semantic tiers against real Postgres/Qdrant/Redis; 63 passed/1 skipped full suite. Scope note: no embedding model exists yet (T1.5 is generation-serving, not embeddings) — `DeterministicStubEmbedder` is an explicitly-flagged placeholder, swappable via the `Embedder` protocol; replace before any MEM-7 eval run. |
| T1.7    | done | Warm context slice in Redis: `MemoryManager.get_warm_context`/`refresh_warm_context`, `tests/integration/test_warm_context.py`. Measured against real Redis/Postgres/Qdrant: `get_warm_context` p50 ≈0.68ms, max ≈1.5ms over 20 samples (budget 100ms), stable over 3 repeated runs. `refresh_warm_context` (the expensive Qdrant+Postgres retrieval) runs post-turn, off the Mode A path, per INV-4. |
| T3.1/T3.4/T3.5 | done (T3.2/T3.3/T3.6 not built — scope note below) | Personality subsystem: `src/personality/user_profile.py` (`UserTrait`/`TraitEvidence` schema per TECHNICAL_ARCHITECTURE.md 7.1, `ProfileManager` with PSU-1 evidence enforcement and PSU-2 confidence-threshold query, `format_traits_for_prompt` injector), `src/personality/agent_character.py` (`load_character_card`, `AgentStance`/`StanceLog` per 7.2 with reversal detection via `revised_from`, heuristic `check_friction`), `src/personality/prompt_assembler.py` (`assemble_system_prompt`, pure/no I/O — INV-4 safe), `personality/character_card.md` placeholder. Tests: `tests/integration/test_user_profile.py`, `tests/integration/test_stance_log.py` (real Postgres), `tests/unit/test_prompt_assembler.py`, `tests/unit/test_agent_character.py` (incl. PSA-1 AST-scan CI check for character-card writes). 22 new tests, 85 passed/1 skipped full suite, stable over 3 runs. Verified PSU-1, reversal-detection, and PSA-1 tests fail correctly when the underlying code is deliberately broken (see MEMORY.md). Scope note: T3.2 (scheduled trait-extraction workers), T3.3 (weekly review UI flow), and T3.6 (full friction-mandate eval, 80% min coverage on 20 conflict scenarios) were not built — `check_friction` here is a minimal heuristic satisfying this task's "identify conflicts" ask, not the T3.6 eval-graded implementation. |

## Rules for agents

1. Read `docs/AGENT_OPERATING_MANUAL.md` section 2 (universal checklist) before completing ANY task
2. Update this file's task tracking table after completing work
3. Update MEMORY.md with architectural decisions made
4. Never modify files in `tests/eval/` without explicit permission
5. Every commit message: `[T{id}] {description} — satisfies {REQ-IDs}`
6. Run `uv run pytest && uv run ruff check src/ && uv run mypy src/` before declaring done
7. Use ECC skills: invoke `python-patterns` for Python code, `python-testing` for tests, `postgres-patterns` for DB work, `security-review` for anything touching auth/crypto/network
8. After significant code changes, let Graphify rebuild the knowledge graph