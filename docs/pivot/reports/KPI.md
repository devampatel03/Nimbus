# KPI — W3 gate

Measured 2026-10-29 on `task/W3-gate-docs` (worktree from `pivot` @ `a1614e8`). Windows 11, `uv sync --extra dev`, compose `postgres` up, `POSTGRES_*` as in CI. No model was loaded; no provider, Google, Notion or Telegram was called. Thresholds: `quality/thresholds.yaml` (unchanged by this PR). Narrative: `docs/pivot/reports/WAVE_W3.md`.

### 1. Test and quality gates

| KPI | Command | Measured | Threshold / target | Status |
|---|---|---|---|---|
| Non-adversarial suite (CI test job) | `uv run pytest --ignore=tests/adversarial -q` | 3280 passed, 5 skipped, 0 failed | `tests_must_pass: true` | met |
| Adversarial tests | `uv run pytest tests/adversarial -q` | 681 passed | `redteam_min_cases: 100` | counts pytest items, not cases; corpus is P4.7 (**not met as a corpus**) |
| Lint | `uv run ruff check .` | All checks passed | `lint_clean: true` | met |
| Types | `uv run mypy src` | Success: no issues in 139 source files | `types_clean: true` | met |
| Coverage | not run at W3 | not measured | `min_coverage_security_policy: 0.95` | not measured |

### 2. Threshold status changes vs W2

| Key | W3 measurement | Status |
|---|---|---|
| `gmail_send_scope_present` | `git grep -E 'gmail\.(send|modify)' -- src config`: 0 hits. `gmail.compose` is an opt-in constant (`src/integrations/google/auth.py:98`), off by default | met in code; real scope set is H16 |
| `oauth_scopes_allowlist_enforced` | OAuth built (P3.4, ADR-0019) | tests pass; no live consent |
| `cost.projected_monthly_inr_max` (1500) | ~4,400-5,550 INR/mo at placeholder prices (STATE P3.3 row; not re-run) | **not met** |
| `daily_spend_cap_enforced` | unchanged from W2 | partly met (WAVE_W2 section 5) |
| Long-term memory written by the running app | `MemoryManager.write_episodic` is called by the `memory_extract` job (`src/scheduler/memory_extract.py`) | wired; tests pass with FakeLLM; no live count |
| `untrusted_auto_promotion` | chunk promotion needs a consumed approval; commitments need a consumed `commitment`/`confirm` approval (trigger `nimbus_ledger_guard`) | tests pass; no runtime count |
| Kill switch operator control | unchanged | no operator control (`/panic` not built) |

### 3. Not measured at W3

LoCoMo (stub pointer 5.58%, ADR-0018, not re-run); real-embedder baseline; provider cost; runtime SLOs; personality harnesses.

---

### W2 gate (history, 2026-10-27)

Measured 2026-10-27 on a fresh worktree of `origin/pivot` @ `bf78d58` (branch `task/W2-gate-docs`). Windows 11, `uv sync --extra dev`, compose `postgres` up (`POSTGRES_*` set as in CI). No model was loaded, no live provider was called, and the LoCoMo and personality harnesses were not re-run. Each number comes from a command run in that worktree. Thresholds: `quality/thresholds.yaml` (values unchanged by this PR). Narrative and risks: `docs/pivot/reports/WAVE_W2.md`.

### 1. Test and quality gates

| KPI | Command | Measured | Threshold / target | Status |
|---|---|---|---|---|
| Unit tests | `uv run pytest tests/unit -q` | 1090 passed, 4 skipped, 0 failed | `tests_must_pass: true` | met |
| Integration tests (real Postgres) | `uv run pytest tests/integration -q` | 151 passed, 1 skipped | `tests_must_pass: true` | met |
| Invariant tests | `uv run pytest tests/eval/invariants -q` | 100 passed | `tests_must_pass: true` | met |
| Non-adversarial suite (CI test job) | `uv run pytest --ignore=tests/adversarial -q --cov=...` | 1341 passed, 5 skipped, 0 failed (1090 + 151 + 100) | `tests_must_pass: true` | met |
| Adversarial tests | `uv run pytest tests/adversarial -q` | 62 passed | `redteam_min_cases: 100` | **not met** (62 of 100). Corpus is P4.7 |
| Lint | `uv run ruff check .` | All checks passed | `lint_clean: true` | met |
| Types | `uv run mypy src` | Success: no issues in 93 source files | `types_clean: true` | met |
| Python source files under `src/` | `git ls-files 'src/*.py'` | 93 (mypy covers the same 93) | n/a | info |
| Test files | `git ls-files 'tests/*.py'` | 97 | n/a | info |

Skips (`uv run pytest tests/unit tests/integration -q -rs`): 3 POSIX permission tests in `tests/unit/app/test_heartbeat.py`, 1 R9 release-gate test (`tests/unit/test_r9_character_card.py`, needs `NIMBUS_REQUIRE_CARD=1`), 1 POSIX-signal test (`tests/integration/app/test_entrypoint_process.py`). The POSIX tests run on Linux CI only.

### 2. Coverage (non-adversarial run, `--cov` on `src.llm src.net src.core src.gateway src.ingest src.app src.orchestrator src.memory`)

| Package | Statements | Missed | Coverage |
|---|---|---|---|
| `src/llm` | 1033 | 24 | 97.7% |
| `src/net` | 356 | 3 | 99.2% |
| `src/core` | 68 | 0 | 100% |
| `src/gateway` | 613 | 22 | 96.4% |
| `src/ingest` | 441 | 10 | 97.7% |
| `src/app` | 703 | 73 | 89.6% |
| `src/orchestrator` | 252 | 7 | 97.2% |
| `src/memory` | 823 | 26 | 96.8% |
| Total of the above (pytest-cov TOTAL) | 4289 | 165 | 96% |

Files below 95%: `src/llm/adapter.py` 90%; `src/app/__main__.py` 0% (one-line shim), `fakes.py` 67%, `cli.py` 84%, `heartbeat.py` 89%, `pipeline.py` 90%, `runtime.py` 92%; `src/memory/embedder.py` 93%, `store.py` 94%, `schemas/vector.py` 68%; `src/orchestrator/context.py` 93%.

`min_coverage_security_policy: 0.95`: `src/llm/scrub.py` 100%. `src/orchestrator/dispatch.py` 96% (only `DenyAllWritesPolicy`). The policy engine, approvals and quarantine are not built, so this threshold is partly applicable.
`min_coverage_changed_lines: 0.80`: not measured (no changed-line coverage computed for this wave).

### 3. Threshold status (`quality/thresholds.yaml`)

| Key | Value in file | W2 measurement | Status |
|---|---|---|---|
| `tests_must_pass` | true | 1341 + 62 pass | met |
| `lint_clean` | true | ruff clean | met |
| `types_clean` | true | mypy clean, 93 files | met |
| `min_coverage_changed_lines` | 0.80 | not measured | not measured |
| `min_coverage_security_policy` | 0.95 | `src/llm` 97.7%, `scrub.py` 100%, `dispatch.py` 96% | partial (policy, approvals, quarantine not built) |
| `max_pr_changed_files_soft` | 40 | PR #26: 62 files (not a pure deletion). PR #20: 29. PR #25: 40 | **exceeded** by PR #26 (merged; process note) |
| `no_secrets_in_diff` | true | detect-secrets hook runs on commit; not re-run in this gate | not re-measured |
| `redteam_min_cases` | 100 | 62 adversarial tests | **not met** |
| `redteam_max_successful_attacks` | 0 | no red-team corpus | n/a |
| `gmail_send_scope_present` | false | `git grep -E 'gmail\.(send|compose|modify)' -- src config`: 0 hits | met in code. Real scope set is H16 |
| `oauth_scopes_allowlist_enforced` | true | OAuth not built (P3.4) | n/a |
| `egress_allowlist_enforced` | true | `src/net` default-deny; `tests/unit/net` passes | met, app-level only |
| `non_allowlisted_user_replies` | 0 | gateway allowlist tests pass (R2); no live count | tests pass |
| `secrets_in_logs` | 0 | INV-7 and R12 tests pass; no leak count measured | tests pass |
| `friction_surfacing_min` 0.80; `anti_sycophancy_*` 0.60 / 0.20; `stance_consistency_ack_min` 0.90 | — | placeholder harnesses, not re-run | not re-measured |
| `character_card_human_authored_marker_required` | true | `grep -c 'human-authored: true' personality/character_card.md` = 0. 2 lines match `PLACEHOLDER`/`TODO(owner)` | **not met** (H1) |
| `provenance_coverage` | 1.0 | R7 test passes; no runtime figure | tests pass |
| `supersession_no_destructive_updates` | true | INV-3 test passes | met |
| `untrusted_auto_promotion` | 0 | chunk promote binding tested; no code creates approvals, no runtime count | tests pass |
| `locomo_min_accuracy_provisional` | 0.70 | NOT MEASURED with a real embedder (stub 5.58%, 2 of 10 samples; ADR-0018) | **not measured** |
| `locomo_target_accuracy` | 0.85 | NOT MEASURED | **not measured** |
| `daily_spend_cap_enforced` | true | R5 guard tests pass (`tests/unit/llm`). **Not met at production defaults:** cap default 200 INR with no upper bound (`src/llm/settings.py:58`) vs `cost.hard_daily_cap_inr` 100; `*` price is a placeholder 250/1000 INR per Mtok (`settings.py:35`); no alert hook wired (`src/app/runtime.py:346-352`); held reservation in memory only | **partly met: tests pass, production wiring gaps (WAVE_W2 §5)** |
| Long-term memory written by the running app | `git grep` for callers of `record_event`, `add_chunks`, `write_semantic` in `src/` (excluding definitions): 0 | 0 production callers; `SessionScopedRecall` always returns empty | **not met** (P3.3) |
| Kill switch operator control | `git grep -i panic -- src`: only the `KILLED` enum value | `llm_kill_switch` is a `system_flag` row (`src/llm/guard.py:73`); `/panic` not built; engaging is manual SQL | tests pass; no operator control |
| `tool_exposure_per_turn_max` | 12 | exposure cap tests pass (`src/orchestrator/exposure.py`) | tests pass |
| `no_lost_updates_after_restart` | true | replay and restart tests pass (P2.1a, P2.1b e2e); no live run | tests pass |
| `runtime.*` (p95 reply, reminder delivery, restart recovery seconds, soak hours) | — | no live app or VM | n/a |
| `ops.*` (restore drill, backup age, runbook, `healthcheck_external_ping_configured`, kill-switch test) | — | not built (P6.x); `healthcheck` CLI exists, ping does not | not built |
| `cost.*` | — | no provider call | n/a |

### 4. LoCoMo (pointer, not re-run)

- Source: `docs/pivot/reports/LOCOMO_REBASELINE_P2.4.md` and ADR-0018. Stub embedder, 2 of 10 samples, top_k 5: 13/233 = 5.58% (both before and after P2.4). Wall time 22.0 s with one embed batch per session (64.6 s before).
- This is plumbing, not retrieval quality. The real-embedder baseline is NOT MEASURED (needs H6 or H3).

### 5. Scope notes

- Test counts vs W1: non-adversarial 694 → 1341 (same three suites plus new ones in `tests/unit/app`, `tests/unit/gateway`, `tests/unit/ingest`, `tests/integration/app` and others). Adversarial 53 → 62.
- The `redteam_min_cases` figure counts adversarial test items, not red-team cases. The 100-case corpus is P4.7.

---

### W1 gate (history, 2026-10-25)

Measured 2026-10-25 on branch `task/W1-gate-docs`, rebased onto `origin/pivot` @ `8c5d083`. Windows 11, `uv sync --extra dev`, compose `postgres` up. No model was loaded and no live provider was called.
Every number below comes from a command run on that checkout (commands listed per table). Thresholds come from `quality/thresholds.yaml`. Narrative and risks: `docs/pivot/reports/WAVE_W1.md`.

## 1. Test and quality gates

| KPI | Command | Measured | Threshold / target | Status |
|---|---|---|---|---|
| Unit tests | `uv run pytest tests/unit` | 528 passed, 0 failed | `tests_must_pass: true` | met |
| Integration tests (real Postgres) | `uv run pytest tests/integration` | 66 passed | `tests_must_pass: true` | met |
| Invariant tests | `uv run pytest tests/eval/invariants` | 100 passed | `tests_must_pass: true` | met |
| Non-adversarial suite (CI test job) | `uv run pytest --ignore=tests/adversarial` | 694 passed, 0 failed, 0 skipped | `tests_must_pass: true` | met |
| Adversarial tests | `uv run pytest tests/adversarial` | 53 passed (53 collected) | `redteam_min_cases: 100` | **not met** (53 of 100). The red-team corpus is P4.7 |
| Lint | `uv run ruff check .` | All checks passed | `lint_clean: true` | met |
| Types | `uv run mypy src` | Success: no issues in 74 source files | `types_clean: true` | met |
| Python source files under `src/` | `find src -name '*.py'` | 74 | n/a | info |

## 2. Coverage (non-adversarial run, `--cov=src.llm --cov=src.net --cov=src.core`)

| Module | Statements | Missed | Coverage | Notes |
|---|---|---|---|---|
| `src/llm` (total) | 852 | 19 | 97.8% | Threshold `min_coverage_security_policy: 0.95` covers policy, approvals, quarantine and scrubber. Policy and quarantine are not built, so this is partial |
| `src/llm/scrub.py` | 80 | 0 | 100% | scrubber (log and prompt scrub) |
| `src/llm/guard.py` | 238 | 5 | 98% | kill switch, spend cap, audit |
| `src/llm/adapter.py` | 142 | 12 | 92% | lowest in `src/llm`, below 95% |
| `src/llm/client.py` | 42 | 1 | 98% | |
| `src/llm/types.py` | 93 | 1 | 99% | |
| `src/llm` other files | 257 | 0 | 100% | `__init__`, factory, fake, pg_stores, pricing, protocols, settings, stores |
| `src/net` (total) | 356 | 3 | 99.2% | `src/net/http.py` 167 statements, 3 missed |
| `src/core` (total) | 68 | 0 | 100% | `src/core/logging.py` |
| Measured total (the three packages) | 1276 | 22 | 98% | pytest-cov TOTAL line |

`min_coverage_changed_lines: 0.80` is not measured here: this PR changes no code lines.

## 3. Threshold status (`quality/thresholds.yaml`)

| Key | Value in file | W1 measurement | Status |
|---|---|---|---|
| `tests_must_pass` | true | 694 + 53 pass | met |
| `lint_clean` | true | ruff clean | met |
| `types_clean` | true | mypy clean, 74 files | met |
| `min_coverage_changed_lines` | 0.80 | not measured (no code lines changed) | n/a |
| `min_coverage_security_policy` | 0.95 | src/llm 97.8%, scrub.py 100% | partial: policy, approvals, quarantine not built |
| `max_pr_changed_files_soft` | 40 | 5 files in this PR | met |
| `no_secrets_in_diff` | true | detect-secrets pre-commit hook passed on the gate commit | met for this commit |
| `redteam_min_cases` | 100 | 53 adversarial tests | **not met** |
| `redteam_max_successful_attacks` | 0 | no red-team harness yet | n/a |
| `gmail_send_scope_present` | false | `grep` for `gmail.send`, `gmail.compose`, `gmail.modify` in `src/` and `config/`: 0 hits | met in code. Scope set itself is H16 |
| `oauth_scopes_allowlist_enforced` | true | OAuth not built | n/a |
| `egress_allowlist_enforced` | true | `src/net` default-deny; `tests/unit/net/test_r13_egress_allowlist.py` passes (app-level only) | met, app-level |
| `non_allowlisted_user_replies` | 0 | gateway (P2.1a) not built | n/a |
| `secrets_in_logs` | 0 | INV-7 tests pass (`test_inv7_no_secrets.py`, `tests/unit/core/test_core_logging.py`). No leak count is measured | tests pass |
| `friction_surfacing_min` | 0.80 | pre-pivot harness, not re-run at W1 | not re-measured |
| `anti_sycophancy_agreement_max` / `stance_consistency_ack_min` | 0.60 / 0.90 | placeholder harnesses, not re-run at W1 | not re-measured |
| `character_card_human_authored_marker_required` | true | `grep -c 'human-authored: true' personality/character_card.md` = 0. Two lines match `PLACEHOLDER`/`TODO(owner)` | **not met** (H1). R9 CI test not built |
| `provenance_coverage` | 1.0 | R7 tests pass (`test_r7_memory_provenance.py`) | tests pass, no runtime figure |
| `supersession_no_destructive_updates` | true | INV-3 tests pass | met |
| `untrusted_auto_promotion` | 0 | R8 not built | n/a |
| `locomo_min_accuracy_provisional` | 0.70 | 6.4286% (stub baseline, see section 4) | **not met**. Not gated until P2.4 |
| `locomo_target_accuracy` | 0.85 | 6.4286% (stub baseline) | **not met** |
| `runtime.*` (p95 reply, reminders, restart, soak, tool exposure) | — | no running app | n/a |
| `ops.*` (restore drill, backups, runbook, healthcheck, kill switch test) | — | not built | n/a |
| `cost.*` | — | no provider call at W1 | n/a |

## 4. LoCoMo stub baseline (pointer, not re-run)

- Source: `docs/pivot/reports/LOCOMO_BASELINE_STUB.md`, "After P1.3b" table.
- Result: overall 6.4286% (99 / 1540), with the sha256 stub embedder and pgvector store. Exact parity with the Qdrant baseline.
- This is not a retrieval-quality number. The real-embedder re-baseline is P2.4.

## 5. Scope notes

- `redteam_min_cases` counts the adversarial tests that exist (53), not a corpus of 100 red-team cases. The 100-case corpus is P4.7.
- Test counts are the same run as section 1. Unit + integration + invariants = 694.
