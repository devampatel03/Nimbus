# WAVE W1 report — Nimbus Lite foundation gate

Date: 2026-10-25. Branch `task/W1-gate-docs` (worktree rebased onto `origin/pivot` @ `8c5d083`, the W1 gate start). Docs-only PR into `pivot`.
Numbers below come only from commands run on that checkout, re-run after the rebase (Windows 11, `uv sync --extra dev`, compose `postgres` up). No model was loaded and no LoCoMo or personality harness was re-run.
KPI table (every number, command and threshold status): `docs/pivot/reports/KPI.md`, created in this PR from the same runs. Thresholds come from `quality/thresholds.yaml`.

## 1. What shipped in W1 (PRs into `pivot`)

| PR | Task | Result (from STATE) |
|---|---|---|
| [#1](https://github.com/devampatel03/Nimbus/pull/1) | P0 audit, coupling, target design (ADRs 0001–0015) | 3 review rounds (73 / 69 / 91%) |
| [#2](https://github.com/devampatel03/Nimbus/pull/2) | P1.2a Postgres image AGE → pgvector | 85%. Static checks only; arm64 image built but not run under emulation |
| [#3](https://github.com/devampatel03/Nimbus/pull/3) | P1.1 CI on pivot | 11/12 |
| [#4](https://github.com/devampatel03/Nimbus/pull/4) | P1.0 delete dead weight | 8/8 |
| [#5](https://github.com/devampatel03/Nimbus/pull/5) | P1.4a `src/net` egress client, R13 | 92% |
| [#6](https://github.com/devampatel03/Nimbus/pull/6) | P1.2b Alembic baseline | 11/11 |
| [#7](https://github.com/devampatel03/Nimbus/pull/7) | P1.2c pivot schema, provenance, append-only triggers | 16/16 after round 2 (round 1: 87.5%) |
| [#8](https://github.com/devampatel03/Nimbus/pull/8) | P1.4b `src/llm` guard, spend cap, kill switch, FakeLLM | 14/14 after 5 rounds (92.9% with 1 HIGH before the last fix). Rounds past the protocol limit of 2 were orchestrator-approved; all were R5 fail-open paths |
| [#9](https://github.com/devampatel03/Nimbus/pull/9) | P1.3a Redis removed | 12/12 |
| [#10](https://github.com/devampatel03/Nimbus/pull/10) | P1.3b pgvector MemoryStore, Qdrant removed, LoCoMo harness ported | 13/14 = 92.9% |
| [#11](https://github.com/devampatel03/Nimbus/pull/11) | P1.4c delete `execute_external` and intent-builder tools; INV-1 rewrite; INV-2 deleted | 18/19 = 94.7% |
| [#12](https://github.com/devampatel03/Nimbus/pull/12) | P1.5 module settings, log scrubbing filter, INV-7, INV-1 parts 1/3 | Merged (b9fb56d), 92.9% (2 rounds: 85.7% -> 92.9%) |
| this PR | W1 gate docs (CLAUDE.md, MEMORY.md, thresholds comment, this report) | see PR body |

## 2. Gate numbers (command output, 2026-10-25)

| Gate | Command | Result | Threshold / target |
|---|---|---|---|
| Unit + integration + invariants | `uv run pytest --ignore=tests/adversarial` | **694 passed**, 0 failed, 0 skipped. Unit 528, integration 66, invariants (`tests/eval/invariants`) 100 | `tests_must_pass: true` — met |
| Adversarial | `uv run pytest tests/adversarial` | **53 passed** | red-team corpus ≥100 cases (`redteam_min_cases`) — **not met**, corpus is P4.7 |
| Lint | `uv run ruff check .` | clean | `lint_clean: true` — met |
| Types | `uv run mypy src` | no issues in **74** source files | `types_clean: true` — met |
| Coverage, src/llm | `uv run pytest --ignore=tests/adversarial --cov=src.llm ...` | **97.8%** (833/852 statements). `scrub.py` 100%, `guard.py` 98%, `client.py` 98% | `min_coverage_security_policy: 0.95` covers policy, approvals, quarantine, scrubber. Policy and quarantine are not built, so this is partial |
| Coverage, src/net | same run | **99.2%** (353/356) | not a listed threshold |
| Coverage, src/core | same run | **100%** (68/68), `logging.py` | not a listed threshold |
| Gmail send scope | `grep` for `gmail.send`/`gmail.compose`/`gmail.modify` in `src/` and `config/` | 0 hits | `gmail_send_scope_present: false` — met for code. Real scope set is H16 |
| Provenance (R7) | `tests/eval/invariants/test_r7_memory_provenance.py` | passing (in the 100 invariants) | `provenance_coverage: 1.0` — tests pass, no runtime coverage figure yet |
| Supersession (INV-3) | `test_inv3_graph_facts_immutable.py` | passing | `supersession_no_destructive_updates: true` — met for the current code |
| Secrets in logs (INV-7) | `test_inv7_no_secrets.py`, `tests/unit/core/test_core_logging.py` | passing. No leak count is measured | `secrets_in_logs: 0` — tests pass, not a measured count |
| LoCoMo (stub baseline) | `docs/pivot/reports/LOCOMO_BASELINE_STUB.md` (not re-run) | **6.43%** (99/1540), pre-real-embedder | `locomo_min_accuracy_provisional: 0.70`, `locomo_target_accuracy: 0.85`. Not gated until P2.4 |
| Character card (R9) | `grep -c 'human-authored: true' personality/character_card.md` | **0** marker; 2 lines match `PLACEHOLDER`/`TODO(owner)` | `character_card_human_authored_marker_required: true` — **not met** (H1). R9 CI test not built |
| Runtime SLOs (p95 reply, reminders, restart, soak) | — | not measurable at W1, no running app | later waves |

Test counts by suite are the same run as above; the 694 figure is the whole non-adversarial suite, which equals the sum of unit, integration and invariants.

## 3. Decisions (this wave)

- ADR-0001 Postgres + pgvector, AGE retired. Accepted.
- ADR-0002 Brief INV numbering canonical. Accepted. H14 approved the protected-test edits on 2026-10-25.
- ADR-0003 `src/llm` replaces `execute_external`. Accepted. Implemented in P1.4b and P1.4c.
- ADR-0004 `src/net` with raw REST and host allow-list. Accepted with deviation from R13 (app-level, not container-level). Human review open (Q3).
- ADR-0005 intent builders deleted, tools never call an LLM. Accepted. Implemented in P1.4c.
- ADR-0006 plain tool loop replaces LangGraph. Accepted. Not built yet (P2.5a/b).
- ADR-0007 Gmail drafts-only cannot be enforced by scope. **Proposed**, H16 open. Blocks P3.4 and P3.6.
- ADR-0008 Postgres SKIP LOCKED jobs. Accepted. Not built yet.
- ADR-0009 policy matrix and approval token. Accepted. Not built yet.
- ADR-0010 lanes and quarantine threshold. **Proposed**, H18 open. Blocks P3.0.
- ADR-0011 reminders outside interruption budget. Proposed default, H17/Q5 open.
- ADR-0012 FastAPI and JWT removed. Accepted. Code removal P2.1b.
- ADR-0013 local Qwen3-Embedding-0.6B. **Provisional** until P2.3 measures arm64.
- ADR-0014 frozen contracts retired. Accepted. Code removal P2.2/P2.5b.
- ADR-0015 character card path `personality/character_card.md`, R9 guard. Accepted.

Pivot-internal decisions: module settings in `src/<module>/settings.py` (`docs/pivot/SETTINGS_PATTERN.md`). Log scrubbing as a `Logger.makeRecord` wrapper (`src/core/logging.py`). INV-1 scanner fails closed on non-literal dynamic imports. Service.LLM is referenced only in `src/llm` and `src/net/policy.py`.

## 4. Documentation changes in this PR

- `CLAUDE.md`: rewritten for Nimbus Lite. Process rules kept (TDD, ADR and MEMORY discipline, commit format, ECC skills, Graphify). Invariants replaced with the Brief §4 set as implemented: INV-1 reframed, INV-2 and INV-4 retired, INV-3 kept, INV-5/6/7 new, R7 and R8 listed. Pre-pivot T-rows collapsed to a pointer, full text at `git show b9fb56d:CLAUDE.md`. Stale `src/security` and `execute_external` references removed. Size 31.5 KB → 8.8 KB.
- `MEMORY.md`: append-only. New dated W1 section (decisions, gate numbers, lessons, superseded list, follow-up table with owners, human items). Retired rows marked `[SUPERSEDED ...]` (AD-1, 2, 3, 7, 8, 9, 12–18, 20–27; interface contracts and API auth headings; tech-debt rows). No history deleted.
- `quality/thresholds.yaml` line 10: comment only, `scrubber` → `llm scrub (src/llm/scrub.py)`. No values changed.

## 5. Open risks

1. No running application yet. Telegram gateway, app entrypoint and the orchestrator loop are not built, so R1–R3 and any end-to-end path are untested.
2. INV-5 (policy), INV-6 (lanes) and R8 (no auto-promotion) are not built. `ChunkHit` does not yet carry lane or untrusted (MED-2).
3. R5 spend cap is not durable. Legacy stores are process-local, and the held reservation is in-memory (lost on restart) until P2.1b.
4. 500/502/503/429 responses are retried under one reservation and not billed. Provider spend may be undercounted until P5.4 measures it.
5. The app DB role `nimbus` is a superuser and can disable the append-only triggers that back INV-3 and R7. **Severity: HIGH until P6.1/P6.2.** Fix: non-superuser app role.
6. Egress is app-level only (`src/net` allow-list). No container or nftables control (P4.5), no post-DNS private-IP check, and ADR-0004 is pending human review.
7. INV-1 parts 4 (runtime zero-transport check) and 5 (`HF_HUB_OFFLINE=1`) are not built. The scanner's known gaps are documented in its docstring (for example `getattr` tricks).
8. Gmail "drafts only" is not enforceable by scope (ADR-0007, H16). Only code-level control is planned.
9. Quality numbers are not real yet: LoCoMo is the 6.43% stub baseline, personality harnesses are placeholders, and no live model has been run in W1 (FakeLLM only).
10. The arm64 image has not been run under emulation (P1.2a / H15 partial).
11. Doc and config drift. `pyproject.toml` still carries fastapi, uvicorn, pyjwt, langgraph, apscheduler and sentence-transformers (removal owners are in the MEMORY.md W1 table; sentence-transformers becomes an optional `[local-embed]` extra). NOTICE still lists Apache AGE, Parakeet-TDT and pyannote entries. `.env.example` has dead VLLM/QDRANT/REDIS lines (H19/H21, human-only).
12. Character card: no `human-authored: true` marker and placeholder text present (H1). The R9 CI test is not built.
13. ADR-0001 and ADR-0014 wording amendments were listed for the wave gate and are not done in this PR (not in its scope).
14. Legacy approver LLM judgment contradicts INV-5. **Severity: HIGH until W2.** `src/orchestrator/approver.py:162-193` (`_llm_judge`) returns `approved` from model output. It is reached from `src/orchestrator/graph.py:220` via `review_async` and built through `GraphDependencies` in `src/api/main.py`. No entrypoint runs it yet. Interim: `DenyAllWritesPolicy` (P2.5a). Removal: P2.5b. Real policy: P3.1.

## 6. Open follow-ups

Full table is in `MEMORY.md`, section "Pivot W1" (35 rows, each with a source and an owner column). Owners are named P-tasks, the human (H19, H21), the orchestrator (NOTICE, ADR wording), or "unassigned" where STATE names no owner. Counts and KPIs: `docs/pivot/reports/KPI.md`.

## 7. Human items at the gate

- Open: H12 (`.claude/settings.json` clobbered in the working tree, HEAD is the original; decide merge or restore), H16 (Gmail scope, ADR-0007), H17 (Q3/Q4/Q5 defaults), H18 (ADR-0010, quarantine all untrusted fields), H19 (`.env.example` VLLM line), H21 (orphaned `nimbus-qdrant-1` and `nimbus-redis-1` containers and volumes; QDRANT/REDIS lines in `.env.example`).
- Resolved: H14 (protected-test batch approval, 2026-10-25, TARGET_DESIGN Q1 scope), H15 (Docker running).
- Also open from the list: H1 (character card), H2–H9 (credentials and infra, needed between W2 and W6).

## 8. Honest limits

- The LoCoMo number above is a pre-pivot stub baseline. It says nothing about retrieval quality with a real embedder.
- "Tests pass" for INV-3, INV-7 and R7 means the current checks pass. It is not a measured leak or violation count.
- No model was loaded and no live provider was called in this gate. Gemini, Telegram and Google are not exercised.
- Coverage is for `src/llm`, `src/net` and `src/core` only. Changed-line coverage was not measured.
- Telegram is not end-to-end encrypted. Provider retention can be up to ~30 days depending on tier. Oracle can reclaim the VM. Prompt injection is bounded, not eliminated (Brief §8).

## 9. Next wave (W2)

W2 task rows P2.1a–P2.6 are in `docs/pivot/STATE.md`: Telegram gateway, app entrypoint, ingest contracts, local embedder with measurement on arm64, retrieval rerank with bi-temporal `as_of`, plain tool loop, and character-card loader with R9 guard. Batch order follows TARGET_DESIGN §9 (not re-verified in this task). The first human inputs needed are H1 (character card) and H2 (Telegram bot token) for W2.
