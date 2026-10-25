# Nimbus — CLAUDE.md

## What is this project

Nimbus is a personal AI assistant that must be 24/7, cheap, human-feeling and "private to a reasonable level" (Brief §1). Since the pivot (2026-10-24) it is **Nimbus Lite**: a Telegram-first bot on one Oracle Cloud Always Free ARM VM, running as one Python process with Postgres + pgvector.
**Architecture authority: `docs/pivot/00_PIVOT_BRIEF.md`.** `docs/PRD.md`, `docs/TECHNICAL_ARCHITECTURE.md` and `docs/EXECUTION_PLAN.md` are superseded for architecture by the Brief (supersede banners are a W7 item).
Read `docs/AGENT_OPERATING_MANUAL.md` and `docs/pivot/02_ORCHESTRATION_PROTOCOL.md` before doing any work.

**Privacy stance (Brief §1, §8):** Telegram, Oracle and the model provider can each see plaintext. We bound leak and injection risk by architecture, not by trusting filters. Never claim more privacy than this.

## Architecture (quick reference, pivot W1 state)

- **Channel:** Telegram bot, long polling, outbound only, allowlisted user ID (R1–R3). Gateway `src/gateway/` is not built yet (P2.1a).
- **Process:** one service (bot + worker + scheduler), entrypoint P2.1b. Still present until removed: `src/api/` (FastAPI/JWT, ADR-0012) and LangGraph in `src/orchestrator/graph.py` (ADR-0006, removal P2.5b).
- **Postgres 16 + pgvector:** `infra/docker/postgres/`, `migrations/` (Alembic), `src/db/` (async client, `crypto.py`), ORM in `src/memory/schemas/`. Append-only triggers on fact/event tables (R7, INV-3). Episodic store: `src/memory/store.py` (`memory_chunk`). Jobs: Postgres `SKIP LOCKED` (ADR-0008, not built).
- **`src/llm` — the only LLM SDK importer (R4, R5).** Guard order: kill switch → scrub (`scrub.py`) → spend cap (R5, reserve-then-bill) → audit row (`llm_audit`) → provider call. Fakes: `src/llm/fake.py`. Settings: `src/llm/settings.py`.
- **`src/net` — the only network transport importer (R13).** Default-deny host allowlist via the `Service` enum in `src/net/policy.py` (ADR-0004).
- **`src/core/logging.py`:** `install_log_scrubbing` (INV-7). Module settings live in `src/<module>/settings.py` (`docs/pivot/SETTINGS_PATTERN.md`); `config/settings.py` keeps only Postgres, auth and CORS.
- **`src/personality/`:** user traits, character-card loader, stance log, friction heuristic. Card: `personality/character_card.md` (ADR-0015).
- **`src/memory/`:** bi-temporal graph (`graph.py`), `manager.py`, `retriever.py`, `embedder.py` (local Qwen3-Embedding-0.6B, provisional, ADR-0013).
- **`src/tools/`:** `BaseTool` and the auto-discovery registry (`registry.py` is the only allow-listed dynamic import). Tasks and reminders are local. Calendar, Gmail (drafts only; ADR-0007 Proposed, pending H16) and Drive are rebuilt in W3 on `src/integrations` (ADR-0005).
- **`src/orchestrator/`:** plain async tool loop with policy and approvals replaces LangGraph and `Approver` (ADR-0006, ADR-0009; not built yet).
- **`src/contracts/`:** pre-pivot frozen models, retired by ADR-0014; removal P2.2/P2.5b.
- **Retired, do not reintroduce:** Redis, Qdrant, Apache AGE, local vLLM/Ollama serving, STT/TTS/diarization, Flutter app, `src/security/` and `execute_external` (deleted P1.4c; scrubbing now in `src/llm/scrub.py`), Gmail send, Gemini-as-executor, intent-builder tools.
- **Tests:** `tests/unit/`, `tests/integration/` (real Postgres), `tests/eval/` (protected, see rule 4), `tests/adversarial/`, `tests/fakes/`, `tests/fixtures/`.

## Commands

- `uv sync --extra dev` — always with `--extra dev`; plain `uv sync` removes the dev tools.
- `docker compose up -d` — starts `postgres` only. Redis, Qdrant and vLLM services are gone.
- `uv run pytest --ignore=tests/adversarial` — CI test job (needs Postgres up).
- `uv run pytest tests/adversarial` — CI red-team job.
- `uv run ruff check .` and `uv run mypy src` — CI lint and types.
- **Deploy:** not built yet (P6.1 arm64 Dockerfile with weights baked in, P6.x runbooks). Never deploy from a task branch.

## Current phase

**Pivot (Nimbus Lite): W1 foundation gate done (2026-10-25). W2 next**: Telegram gateway and app entrypoint (P2.1a/b), ingest (P2.2), embedder (P2.3), retrieval (P2.4), orchestrator loop (P2.5a/b), character-card loader (P2.6).
Task table: `docs/pivot/STATE.md`. W1 numbers and open items: `docs/pivot/reports/WAVE_W1.md`.

## Invariants (Brief §4, as implemented; tested in CI, never violate)

- **INV-1 (reframed):** exactly one module calls an LLM (`src/llm`), and only `src/net` imports a network transport. Enforced by `tests/unit/llm/test_llm_import_lint.py` and `tests/eval/invariants/test_inv1_single_egress_point.py` (import scan, fails closed on non-literal dynamic imports; allow-list is `src/net/`→httpx and `src/tools/registry.py`→dynamic-import; `Service.LLM` is referenced only under `src/llm` and `src/net/policy.py`). Not built yet: the runtime zero-transport check (part 4) and the `HF_HUB_OFFLINE=1` assertion (part 5, P2.3).
- **INV-2: RETIRED** (was: no memory in egress payload / local-only inference). Number never reused.
- **INV-3:** graph facts are append-only; supersession only. `tests/eval/invariants/test_inv3_graph_facts_immutable.py` (AST scan) plus DB triggers.
- **INV-4: RETIRED** (was: Mode A never blocks on network; mode A/B removed).
- **INV-5 (new):** policy decisions are deterministic code; no LLM output grants a permission. Not built yet (P3.1) **Currently violated** by the legacy approver until P2.5b (see Known violations below).
- **INV-6 (new):** untrusted text enters the trusted lane only as schema-validated, delimited DATA. Not built yet (P3.0; blocked on H18 / ADR-0010).
- **INV-7 (new):** no secret or token value in logs, prompts, memory, audit rows or error messages. Covered by the log filter, `src/llm/scrub.py` and hidden settings values (`tests/eval/invariants/test_inv7_no_secrets.py`). DB canary sweep and obfuscation cases: P4.4.
- **R7 (provenance; was repo INV-5):** every derived memory row has non-null provenance (`tests/eval/invariants/test_r7_memory_provenance.py`).
- **R8 (untrusted memory never auto-promoted):** not built (P3.0, P4.6).
- **Known violations, with severity:**
  - **HIGH, until W2 (INV-5):** legacy `src/orchestrator/approver.py` `_llm_judge` (lines 162-193) returns `approved` taken from model output. It is reached from `src/orchestrator/graph.py:220` (`review_async`) and built through `GraphDependencies` in `src/api/main.py`. No entrypoint runs it yet. Interim fix: `DenyAllWritesPolicy` (P2.5a). Removal: P2.5b. Real policy: P3.1.
  - **HIGH, until P6.1/P6.2 (INV-3, R7):** the app DB role `nimbus` is a superuser and can disable the append-only triggers. Fix: non-superuser app role.

## Conventions

- **TDD:** failing test first. For security guards, prove the test can fail (break the code, or monkeypatch in-test). Never leave a weakened security file on disk.
- **Decisions:** architecture changes get an ADR in `docs/decisions/` (next: 0016) and a dated MEMORY.md entry. Thresholds in `quality/thresholds.yaml` change only via ADR plus reviewer sign-off.
- **Git:** task branches `task/<id>-<slug>` off `pivot`; PRs target `pivot`. Stage explicit paths only (`git add <path>`), never `git add -A`.
- **Windows:** the working tree is CRLF (`core.autocrlf`); check `git diff --stat` before committing. Commit hooks (mypy env build) are slow and memory-heavy here; do not run them in parallel.

## Never do

- Never push to or merge into `main`. The final `pivot`→`main` merge is a human step (H10).
- Never add a Gmail send scope or a send path (R6; ADR-0007 pending H16).
- Never import an LLM SDK outside `src/llm`, or a transport outside `src/net` (INV-1).
- Never add a model call that bypasses `src/llm` (R4).
- Never let model output grant a permission (INV-5).
- Never edit the character card (`personality/character_card.md`, ADR-0015).
- Never log, print or commit a secret (INV-7, R12). Agents do not read `.env.*` files.
- Never widen the `Service` allow-list without an ADR (R13).
- Never overstate privacy beyond Brief §8 (not E2EE; provider retention; Oracle reclaim; injection bounded, not eliminated).

## Task tracking

- Live table: `docs/pivot/STATE.md` (orchestrator-owned).
- Pre-pivot T-rows (T0.1–T4.x, Step 11 and the "this session" rows) are SUPERSEDED by the pivot task table. Full text: `git show b9fb56d:CLAUDE.md`.
- Pivot status: P0 (PR #1), P1.0 (#4), P1.1 (#3), P1.2a (#2, arm64 not run), P1.2b (#6), P1.2c (#7), P1.3a (#9), P1.3b (#10), P1.4a (#5), P1.4b (#8), P1.4c (#11), P1.5 (#12). W1 gate: see `docs/pivot/reports/WAVE_W1.md`.

## Rules for agents

1. Read `docs/AGENT_OPERATING_MANUAL.md` section 2 (universal checklist) before completing ANY task.
2. Update the task table (`docs/pivot/STATE.md`) after completing work.
3. Update MEMORY.md with architectural decisions made (append-only, dated).
4. Never modify files in `tests/eval/` without explicit permission. The H14 batch approval (2026-10-25) covers only the TARGET_DESIGN Q1 scope.
5. Commit message: `[P<wave>.<n>] description — satisfies <REQ/INV ids>`. Gate commits: `[W<n>-gate] description — documents <REQ/INV ids>`.
6. Run `uv run pytest --ignore=tests/adversarial && uv run ruff check . && uv run mypy src` before declaring done (plus `uv run pytest tests/adversarial` when touching red-team code).
7. Use ECC skills: `python-patterns` for Python, `python-testing` for tests, `postgres-patterns` for DB work, `security-review` for anything touching auth, crypto or network.
8. After significant code changes, let Graphify rebuild the knowledge graph (`/graphify`).
