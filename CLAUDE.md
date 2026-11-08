# Nimbus — CLAUDE.md

## What is this project

Nimbus is a personal AI assistant that must be 24/7, cheap, human-feeling and "private to a reasonable level" (Brief §1). Since the pivot (2026-10-24) it is **Nimbus Lite**: a Telegram-first bot on one Oracle Cloud Always Free ARM VM, running as one Python process with Postgres + pgvector.
**Architecture authority: `docs/pivot/00_PIVOT_BRIEF.md`.** `docs/PRD.md`, `docs/TECHNICAL_ARCHITECTURE.md` and `docs/EXECUTION_PLAN.md` are superseded for architecture by the Brief (supersede banners are a W7 item).
Read `docs/AGENT_OPERATING_MANUAL.md` and `docs/pivot/02_ORCHESTRATION_PROTOCOL.md` before doing any work.

**Privacy stance (Brief §1, §8):** Telegram, Oracle and the model provider can each see plaintext. We bound leak and injection risk by architecture, not by trusting filters. Never claim more privacy than this.

## Architecture (quick reference, pivot W5 state)

- **Channel:** `src/gateway/` Telegram long polling, outbound only, allowlisted user ID (R1–R3). At-least-once, bounded redelivery; consumer dedupes on `telegram:<update_id>` (ADR-0016). Exfil filter on sends (P4.3).
- **Process:** `src/app/`, one process, three supervised loops: gateway (`Ingestor.run`, turn queue), worker (job runner: reminders and `memory_extract`, plus `recover_pending`), scheduler (`MemoryExtractSweeper`; proactive jobs, P5.1, no model call). Heartbeat file read by `healthcheck` (ADR-0012). Log scrubbing first (R12). Google/Notion groups bind only with credentials (`src/app/integrations.py`).
- **Postgres 16 + pgvector:** `infra/docker/postgres/`, `migrations/` (Alembic, head `e5b1c7a3d902`), `src/db/`, ORM in `src/memory/schemas/`. Append-only triggers on fact, event, `policy_audit` and `commitment_ledger` tables (R7, INV-3); promotion guard `nimbus_promotion_guard` on `memory_chunk`/`graph_edge` (R8, P4.6). The app role is a superuser (HIGH, see Known violations).
- **`src/llm` — the only LLM SDK importer (R4, R5).** Guard order: kill switch → lane → scrub → rate limiter → spend cap (`CallAccountant`, shared with `ApiEmbedder`) → audit row → provider call.
- **`src/net` — the only network transport importer (R13).** Default-deny host allowlist, `Service` enum in `src/net/policy.py` (ADR-0004). Post-DNS address check (P4.5).
- **`src/core/logging.py`:** `install_log_scrubbing` (INV-7). Module settings: `src/<module>/settings.py` (`docs/pivot/SETTINGS_PATTERN.md`).
- **`src/personality/`:** every card load goes through `load_approved_character_card` (R9, ADR-0015).
- **`src/memory/`:** `store.py` episodic chunks (ADR-0017); `graph.py` bi-temporal facts (supersede only); `commitments.py` commitment ledger (ADR-0022, Proposed); `review.py` ReviewQueue (P4.6); `embedder.py` (local Qwen3, provisional, ADR-0013).
- **`src/policy/`, `src/approvals/`, `src/lanes/`, `src/orchestrator/`:** table-driven `PolicyEngine` (ADR-0009) wired into dispatch, creates denied (`PENDING_IDEMPOTENCY`), decisions in `policy_audit`. HMAC single-use approvals (ADR-0020, Proposed; H32). Canary tripwires (`src/canary/`, ADR-0023) screen replies, args, sends, egress, logs. Untrusted text reaches the model only as DATA via `Quarantine.extract` (budget 10 per turn). Loop ≤12 tools (ADR-0006).
- **`src/tools/`:** Tool protocol v2 and registry (`registry.py`). Calendar, Drive, Notion built; Gmail not built (P3.6, H16, ADR-0007 Proposed). External-service output is untrusted.
- **Removed in W2, do not reintroduce:** `src/api/`, FastAPI, uvicorn, pyjwt (ADR-0012); LangGraph, `graph.py`, `approver.py` (ADR-0006). Contracts: Event, Transcript, MemoryQuery, UserProfileSlice (ADR-0014).
- **Retired, do not reintroduce:** Redis, Qdrant, Apache AGE, local vLLM/Ollama, STT/TTS/diarization, Flutter, `src/security/`, `execute_external`, Gmail send, Gemini-as-executor, intent-builder tools.
- **Tests:** `tests/` unit, integration (real Postgres), `eval/` (protected, rule 4), `tests/adversarial/` (red team), fakes, fixtures.

## Commands

- `uv sync --extra dev` — always with `--extra dev`; plain `uv sync` removes the dev tools. `--extra local-embed` only where the local embedder runs.
- `docker compose up -d` — starts `postgres` only.
- `uv run python -m src.app` — runs the bot. Needs Telegram and LLM settings from the environment. Fake backends need both `APP_FAKE_BACKENDS=true` and `APP_ENV=development`; anything else is refused.
- `uv run python -m src.app healthcheck` — exit 0 healthy, 1 unhealthy (stale, missing or malformed heartbeat). `run` exits 2 on startup refused, 3 on embedding migration required, 4 on a fatal loop error.
- `uv run pytest --ignore=tests/adversarial` (CI test job) and `uv run pytest tests/adversarial` (red team); both need Postgres up. Each session creates and drops its own `nimbus_test_<epoch>_<pid>_<uuid8>` DB (INFRA-1, PR #17); stale ones older than 6 h are dropped at start.
- `uv run ruff check .` and `uv run mypy src` — CI lint and types.
- **Deploy:** not built yet (P6.1 arm64 Dockerfile, P6.x runbooks). Never deploy from a task branch.

## Current phase

**Pivot (Nimbus Lite): W4 gate docs open (`docs/pivot/reports/WAVE_W4.md`); W5 in progress.** Merged: P4.1, P4.3–P4.6, P5.1, P5.5 groundwork (PRs #46–#55). Not built: P4.7 (protected corpus, needs approval), P5.2, P3.6 (H16). Nothing ran live. No live run with production credentials until the superuser-role HIGH is fixed (P6.1) or waived.
Task table: `docs/pivot/STATE.md`.

## Invariants (Brief §4, as implemented; tested in CI, never violate)

- **INV-1 (reframed):** only `src/llm` calls an LLM and only `src/net` imports a transport. Enforced by `tests/unit/llm/test_llm_import_lint.py` and `tests/eval/invariants/test_inv1_single_egress_point.py` (import scan, fails closed on dynamic imports). Zero-transport: `tests/unit/llm/test_llm_guard_r4.py`. `HF_HUB_OFFLINE=1` in `src/memory/embedder.py`.
- **INV-2: RETIRED** (was: no memory in egress payload / local-only inference). Number never reused.
- **INV-3:** graph facts are append-only; supersession only. `tests/eval/invariants/test_inv3_graph_facts_immutable.py` (AST scan) plus DB triggers.
- **INV-4: RETIRED** (was: Mode A never blocks on network; mode A/B removed).
- **INV-5:** no LLM output grants a permission. `PolicyEngine` decides (P3.1); `user_confirmed` comes only from a sealed `ConsumedApproval` (P3.2, ADR-0020). Tests: `tests/eval/invariants/test_inv5_*.py`, `tests/unit/orchestrator/test_inv5_no_llm_permission.py`.
- **INV-6 (built, PRs #29, #43):** untrusted text enters the trusted lane only as schema-validated DATA; Calendar and Notion give the model opaque handles only. Tests: `tests/eval/invariants/test_inv6_*.py`, `tests/adversarial/lanes/`. Interim: quarantine every untrusted field (ADR-0010, H18). Lane default `'trusted'` stays on three tables (H23).
- **INV-7:** no secret or token value in logs, prompts, memory, audit rows or errors. Covered by the log filter, `src/llm/scrub.py`, hidden settings and `hide_parameters` (`tests/eval/invariants/test_inv7_no_secrets.py`). Canary tripwires and `canary-sweep` (ADR-0023): not boundaries.
- **R7 (provenance; was repo INV-5):** every derived memory row has non-null provenance (`tests/eval/invariants/test_r7_memory_provenance.py`).
- **R8 (untrusted memory never auto-promoted):** chunk promotion needs a consumed approval (migration `c4e8a1d92b37`). Confirmed commitments need a consumed `commitment`/`confirm` approval (trigger `nimbus_ledger_guard`, ADR-0022). Graph-edge promotion: same rule (trigger `nimbus_promotion_guard`, ADR-0024).
- **R12 (no secrets in logs):** met at the entrypoint: `src/app/runtime.py` calls `install_log_scrubbing(cfg.secrets)` before anything else logs (P2.1b).
- **Known violations, with severity:**
  - **HIGH, until P6.1/P6.2 (INV-3, R7):** the app DB role `nimbus` is a superuser and can disable the append-only triggers. **Mitigated (P6.1, ADR-0026) once `POSTGRES_APP_USER/PASSWORD` are set** (runtime uses non-owner `nimbus_app`; `db-bootstrap` grants; real-Postgres tests). With one DSN it still applies and the app logs a WARNING. Confirm the startup line before a live run.
  - **Open, staging-blocking (WAVE_W3 §5):** projected cost over `cost.projected_monthly_inr_max` 1500 at placeholder prices (WAVE_W3 §5; H3).
  - **Open, not HIGH (WAVE_W2 §5):** spend cap default 50 INR, max 100 (ADR-0025 Proposed); `*` price is a placeholder; no alert hook; kill switch is a `system_flag` row, no `/panic`. Before any staging run with a real key: real model price (`LLM_PRICE_*`).

## Conventions

- **TDD:** failing test first. For security guards, prove the test can fail (break the code, or monkeypatch in-test). Never leave a weakened security file on disk.
- **Decisions:** architecture changes get an ADR in `docs/decisions/` (next: 0026) and a dated MEMORY.md entry. Thresholds in `quality/thresholds.yaml` change only via ADR plus reviewer sign-off.
- **Git:** task branches `task/<id>-<slug>` off `pivot`; PRs target `pivot`. Stage explicit paths only (`git add <path>`), never `git add -A`.
- **Windows:** working tree is CRLF (`core.autocrlf`); check `git diff --stat` before committing. Do not run commit hooks in parallel.

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
- Pre-pivot T-rows are SUPERSEDED by the pivot task table (`git show b9fb56d:CLAUDE.md`).
- Pivot PRs: W1 #1–#13, W2 #14–#27, W3 #28–#45, W4–W5 #46–#55 (`docs/pivot/reports/WAVE_W*.md`).

## Rules for agents

1. Read `docs/AGENT_OPERATING_MANUAL.md` section 2 (universal checklist) before completing ANY task.
2. Update the task table (`docs/pivot/STATE.md`) after completing work.
3. Update MEMORY.md with architectural decisions made (append-only, dated).
4. Never modify files in `tests/eval/` without explicit permission. The H14 batch approval (2026-10-25) covers only the TARGET_DESIGN Q1 scope.
5. Commit message: `[P<wave>.<n>] description — satisfies <REQ/INV ids>`. Gate commits: `[W<n>-gate] description — documents <REQ/INV ids>`.
6. Run `uv run pytest --ignore=tests/adversarial && uv run ruff check . && uv run mypy src` before declaring done (plus `uv run pytest tests/adversarial` when touching red-team code).
7. Use ECC skills: `python-patterns` for Python, `python-testing` for tests, `postgres-patterns` for DB work, `security-review` for anything touching auth, crypto or network.
8. After significant code changes, let Graphify rebuild the knowledge graph (`/graphify`).
