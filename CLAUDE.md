# Nimbus — CLAUDE.md

## What is this project

Nimbus is a personal AI assistant that must be 24/7, cheap, human-feeling and "private to a reasonable level" (Brief §1). Since the pivot (2026-10-24) it is **Nimbus Lite**: a Telegram-first bot on one Oracle Cloud Always Free ARM VM, running as one Python process with Postgres + pgvector.
**Architecture authority: `docs/pivot/00_PIVOT_BRIEF.md`.** `docs/PRD.md`, `docs/TECHNICAL_ARCHITECTURE.md` and `docs/EXECUTION_PLAN.md` are superseded for architecture by the Brief (supersede banners are a W7 item).
Read `docs/AGENT_OPERATING_MANUAL.md` and `docs/pivot/02_ORCHESTRATION_PROTOCOL.md` before doing any work.

**Privacy stance (Brief §1, §8):** Telegram, Oracle and the model provider can each see plaintext. We bound leak and injection risk by architecture, not by trusting filters. Never claim more privacy than this.

## Architecture (quick reference, pivot W2 state)

- **Channel:** `src/gateway/` Telegram long polling, outbound only, allowlisted user ID (R1–R3). Offset advances on ack, at-least-once, bounded redelivery; consumer dedupes on `telegram:<update_id>` (ADR-0016).
- **Process:** `src/app/` is the single process: gateway, worker (ingest, recovery) and scheduler loops, supervised. Heartbeat file read by `healthcheck` (ADR-0012). Log scrubbing installed first (R12).
- **Postgres 16 + pgvector:** `infra/docker/postgres/`, `migrations/` (Alembic, head `c4e8a1d92b37`), `src/db/`, ORM in `src/memory/schemas/`. Append-only triggers on fact and event tables (R7, INV-3).
- **`src/llm` — the only LLM SDK importer (R4, R5).** Guard order: kill switch → scrub (`scrub.py`) → spend cap (`CallAccountant`, shared by `GuardedLLM` and `ApiEmbedder`) → audit row (`llm_audit`) → provider call.
- **`src/net` — the only network transport importer (R13).** Default-deny host allowlist, `Service` enum in `src/net/policy.py` (ADR-0004).
- **`src/core/logging.py`:** `install_log_scrubbing` (INV-7). Module settings in `src/<module>/settings.py` (`docs/pivot/SETTINGS_PATTERN.md`).
- **`src/personality/`:** card loader `load_approved_character_card` (R9 gate, ADR-0015). Every card load goes through it.
- **`src/memory/`:** `store.py` episodic chunks (ADR-0017: `record_event`, `add_chunks`, `search` with `as_of`/`known_at`, `promote`); `graph.py` bi-temporal facts (supersede only); `embedder.py` (local Qwen3-Embedding-0.6B, provisional, ADR-0013).
- **`src/orchestrator/`:** plain async tool loop (`loop.py`, ≤12 tools, caps; ADR-0006). `dispatch.py` holds `DenyAllWritesPolicy`: every write is denied until the real policy (P3.1, ADR-0009).
- **`src/tools/`:** Tool protocol v2 (`base.py`) and the auto-discovery registry (`registry.py`, the only allow-listed dynamic import). Calendar, Gmail (drafts only; ADR-0007 Proposed, H16) and Drive: W3 (ADR-0005).
- **`src/contracts/`:** Event, Transcript, MemoryQuery/Result, UserProfileSlice, ProactiveCandidate (ADR-0014).
- **Removed in W2, do not reintroduce:** `src/api/`, FastAPI, uvicorn, pyjwt (ADR-0012, P2.1b); LangGraph, `graph.py`, `approver.py` (ADR-0006, P2.5b).
- **Retired, do not reintroduce:** Redis, Qdrant, Apache AGE, local vLLM/Ollama, STT/TTS/diarization, Flutter, `src/security/`, `execute_external`, Gmail send, Gemini-as-executor, intent-builder tools.
- **Tests:** `tests/unit/`, `tests/integration/` (real Postgres), `tests/eval/` (protected, rule 4), `tests/adversarial/`, `tests/fakes/`, `tests/fixtures/`.

## Commands

- `uv sync --extra dev` — always with `--extra dev`; plain `uv sync` removes the dev tools. `--extra local-embed` only where the local embedder runs.
- `docker compose up -d` — starts `postgres` only.
- `uv run python -m src.app` — runs the bot. Needs Telegram and LLM settings from the environment. `APP_ENV=development` enables fake backends for local runs only (refused in production).
- `uv run python -m src.app healthcheck` — exit 0 healthy, 1 unhealthy (stale, missing or malformed heartbeat). `run` exits 2 on startup refused, 3 on embedding migration required, 4 on a fatal loop error.
- `uv run pytest --ignore=tests/adversarial` — CI test job (needs Postgres up). Each session creates and drops its own `nimbus_test_<epoch>_<pid>_<uuid8>` database (INFRA-1, PR #17); stale ones older than 6 h are dropped at start.
- `uv run pytest tests/adversarial` — CI red-team job (needs Postgres).
- `uv run ruff check .` and `uv run mypy src` — CI lint and types.
- **Deploy:** not built yet (P6.1 arm64 Dockerfile, P6.x runbooks). Never deploy from a task branch.

## Current phase

**Pivot (Nimbus Lite): W2 built and gated (2026-10-27, `docs/pivot/reports/WAVE_W2.md`). W3 next:** lanes and quarantine (P3.0, blocked on H18), policy (P3.1), approvals (P3.2), scheduler (P3.3), tools (P3.4–P3.8).
Task table: `docs/pivot/STATE.md`.

## Invariants (Brief §4, as implemented; tested in CI, never violate)

- **INV-1 (reframed):** exactly one module calls an LLM (`src/llm`), and only `src/net` imports a network transport. Enforced by `tests/unit/llm/test_llm_import_lint.py` and `tests/eval/invariants/test_inv1_single_egress_point.py` (import scan, fails closed on dynamic imports; known gaps in its docstring). Zero-transport check: `tests/unit/llm/test_llm_guard_r4.py`. `HF_HUB_OFFLINE=1` enforced in `src/memory/embedder.py`.
- **INV-2: RETIRED** (was: no memory in egress payload / local-only inference). Number never reused.
- **INV-3:** graph facts are append-only; supersession only. `tests/eval/invariants/test_inv3_graph_facts_immutable.py` (AST scan) plus DB triggers.
- **INV-4: RETIRED** (was: Mode A never blocks on network; mode A/B removed).
- **INV-5:** policy decisions are deterministic code; no LLM output grants a permission. Regression: `tests/unit/orchestrator/test_inv5_no_llm_permission.py` (AST scan). Until P3.1 the policy is `DenyAllWritesPolicy`, so no write runs. The scanner is a name tripwire; P3.1 adds a structural test.
- **INV-6:** untrusted text enters the trusted lane only as schema-validated, delimited DATA. Not built (P3.0; blocked on H18 / ADR-0010). Tool output defaults to `untrusted=True`; framing is prompt-only until P3.0.
- **INV-7:** no secret or token value in logs, prompts, memory, audit rows or error messages. Covered by the log filter, `src/llm/scrub.py`, hidden settings values and `hide_parameters` (`tests/eval/invariants/test_inv7_no_secrets.py`). DB canary sweep: P4.4.
- **R7 (provenance; was repo INV-5):** every derived memory row has non-null provenance (`tests/eval/invariants/test_r7_memory_provenance.py`).
- **R8 (untrusted memory never auto-promoted):** enforced for chunk promotion only: a consumed approval for `tool=memory, action=promote, item_id`, single use (migration `c4e8a1d92b37`). Graph-edge promotion has no store method (P4.6). No code creates approvals; quarantine (P3.0) and review queue (P4.6) are not built.
- **R12 (no secrets in logs):** met at the entrypoint: `src/app/runtime.py` calls `install_log_scrubbing(cfg.secrets)` before anything else logs (P2.1b).
- **Known violations, with severity:**
  - **HIGH, until P6.1/P6.2 (INV-3, R7):** the app DB role `nimbus` is a superuser and can disable the append-only triggers. Fix: non-superuser app role.

## Conventions

- **TDD:** failing test first. For security guards, prove the test can fail (break the code, or monkeypatch in-test). Never leave a weakened security file on disk.
- **Decisions:** architecture changes get an ADR in `docs/decisions/` (next: 0019) and a dated MEMORY.md entry. Thresholds in `quality/thresholds.yaml` change only via ADR plus reviewer sign-off.
- **Git:** task branches `task/<id>-<slug>` off `pivot`; PRs target `pivot`. Stage explicit paths only (`git add <path>`), never `git add -A`.
- **Windows:** the working tree is CRLF (`core.autocrlf`); check `git diff --stat` before committing. Commit hooks (mypy env build) are slow; do not run them in parallel.

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
- Pivot PRs: W1 #1–#13 (`docs/pivot/reports/WAVE_W1.md`); W2 #14–#26 (`docs/pivot/reports/WAVE_W2.md`).

## Rules for agents

1. Read `docs/AGENT_OPERATING_MANUAL.md` section 2 (universal checklist) before completing ANY task.
2. Update the task table (`docs/pivot/STATE.md`) after completing work.
3. Update MEMORY.md with architectural decisions made (append-only, dated).
4. Never modify files in `tests/eval/` without explicit permission. The H14 batch approval (2026-10-25) covers only the TARGET_DESIGN Q1 scope.
5. Commit message: `[P<wave>.<n>] description — satisfies <REQ/INV ids>`. Gate commits: `[W<n>-gate] description — documents <REQ/INV ids>`.
6. Run `uv run pytest --ignore=tests/adversarial && uv run ruff check . && uv run mypy src` before declaring done (plus `uv run pytest tests/adversarial` when touching red-team code).
7. Use ECC skills: `python-patterns` for Python, `python-testing` for tests, `postgres-patterns` for DB work, `security-review` for anything touching auth, crypto or network.
8. After significant code changes, let Graphify rebuild the knowledge graph (`/graphify`).
