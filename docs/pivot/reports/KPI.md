# KPI — W1 gate

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
