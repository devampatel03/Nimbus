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
| `SessionBundle` | `src/contracts/session_bundle.py` | 1 | 2026-09-12 |
| `DiarizedTranscript` (+ `Utterance`) | `src/contracts/diarized_transcript.py` | 1 | 2026-09-12 |
| `MemoryQuery` / `MemoryResult` | `src/contracts/memory_query.py` | 1 | 2026-09-12 |
| `UserProfileSlice` (+ `TraitSummary`, `StanceSummary`) | `src/contracts/user_profile_slice.py` | 1 | 2026-09-12 |
| `ToolIntent` / `ToolResult` (+ `RiskClass`, `Primitive`) | `src/contracts/tool_intent.py` | 1 | 2026-09-12 |
| `ProactiveCandidate` | `src/contracts/proactive_candidate.py` | 1 | 2026-09-12 |

Golden fixtures: `tests/fixtures/*.json`. Compatibility test (fails on field removal or type change): `tests/unit/test_contracts.py`.

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
- 2026-09-12 (T0.2): Process note — `docs/EXECUTION_PLAN.md`'s actual T0.2 is the P-SEC pod task (server provisioning: LUKS, WireGuard, egress proxy, no public ingress), which is completely different from the local-dev-infra work described in this session's task instruction. Flagged the mismatch per AGENT_OPERATING_MANUAL's "spec is right, raise it, don't edit it" rule; user explicitly replied "proceed with the local dev infra work." The work below was done under the `[T0.2]` label per that instruction, but `docs/EXECUTION_PLAN.md`'s real T0.2 (P-SEC provisioning) is still outstanding and not reconciled.
- Postgres image: `infra/docker/postgres/Dockerfile` builds on `apache/age:release_PG16_1.5.0` and installs `postgresql-16-pgvector` via apt, since no single upstream image ships both Apache AGE and pgvector together.
- mypy strict-mode gotcha: setting `[tool.mypy] python_version = "3.11"` restricts mypy's own *parser* to 3.11 syntax for every file it walks, including third-party stubs — this broke on `numpy` 2.5.x stubs which use PEP 695 `type` statement syntax unconditionally. Fix was to drop the explicit `python_version` pin entirely (let mypy infer from the running interpreter, which is 3.14 in this project's `.venv`), not to add per-module overrides or `exclude` patterns (both were tried and didn't work — `follow_imports = "skip"` doesn't prevent the stub file from being parsed).
- mypy "Source file found twice under different module names" (e.g. `db.postgres` vs `src.db.postgres`) was fixed by adding real `__init__.py` markers at `src/`, `src/api/`, and `config/` (matching the existing `src/db/__init__.py`), not by `explicit_package_bases`/`mypy_path` — that combination incorrectly made `.venv` a searchable import root and surfaced the numpy stub error above.
- pytest needs `pythonpath = ["."]` under `[tool.pytest.ini_options]` in `pyproject.toml` for `from src....` / `from config....` imports to resolve in test modules under `tests/unit/` (no root `conftest.py` exists to do this implicitly).
- 2026-09-12 (T0.8/T0.9): `docs/TECHNICAL_ARCHITECTURE.md` section 12 only gives the contract *names* and producer/consumer/freeze-phase, not field-level schemas (except `ToolIntent`, spelled out in section 8.1). Fields for the other five contracts were derived from context elsewhere in the same doc (mode state machine, Mode A latency budget, memory bi-temporal schema, personality subsystem, proactive engine) — a defensible but not spec-mandated design; a pod that later needs a field these don't have should amend via review, not silently add it.
- Compatibility-test technique used in `tests/unit/test_contracts.py`: record each model's `model_fields` names + a normalized string repr of their `annotation` in a hardcoded `EXPECTED_FIELDS` dict, then assert equality every run. This catches field removal and type changes without needing to snapshot a JSON schema file; verified by deliberately deleting a field and confirming the test fails with a clear message, then restoring it.
- `docs/EXECUTION_PLAN.md`'s actual T0.9 acceptance criterion is "SBOM generated in CI; Parakeet CC-BY flagged in NOTICE" — this session's task instruction only asked for the NOTICE file, not CI SBOM automation. Flagged per the "spec is right, raise it" rule (same pattern as the T0.2 mismatch); T0.9 is marked `partial` in CLAUDE.md's task table pending that follow-up, rather than mislabeling it `done`.
- Package licences for the NOTICE file were confirmed via `importlib.metadata` on the installed packages (`License-Expression` / `Classifier` fields), not recalled from memory — several packages (fastapi, pydantic, langgraph, pytest, ruff, mypy) only expose licence via the newer `License-Expression` metadata field, not the legacy `License:` header.