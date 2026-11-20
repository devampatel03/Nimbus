# WAVE W2 report — Nimbus Lite build wave, gate

Date: 2026-10-27. Branch `task/W2-gate-docs`, worktree from `origin/pivot` @ `bf78d58`. Docs-only PR into `pivot`.
Numbers come only from commands run on that checkout (fresh worktree, Windows 11, `uv sync --extra dev`, compose `postgres` up). KPI table with every command and threshold status: `docs/pivot/reports/KPI.md`. No model was loaded, no live provider was called, and the LoCoMo and personality harnesses were not re-run.

## 1. What shipped in W2 (PRs into `pivot`)

Dates are UTC from GitHub `mergedAt`. Scores and rounds are from STATE; "not recorded" means STATE has no score for that PR.

| PR | Task | Merged (UTC), merge SHA | Result (STATE) | Review rounds |
|---|---|---|---|---|
| [#14](https://github.com/devampatel03/Nimbus/pull/14) | P2.1a Telegram long-poll gateway (R1–R3, ADR-0016) | 2026-10-25 23:44, `1eb8808` | 71% → 93% | 2 |
| [#15](https://github.com/devampatel03/Nimbus/pull/15) | P2.5a orchestrator loop, exposure cap, `DenyAllWritesPolicy` | 2026-10-26 00:25, `1b497fa` | 71% → 100% | 3 passes (round 3 orchestrator-approved) |
| [#16](https://github.com/devampatel03/Nimbus/pull/16) | P2.3 batch/async Qwen3 embedder, HF offline, guarded API fallback | 2026-10-26 00:34, `f171605` | 64% → 92.9% | 2 |
| [#17](https://github.com/devampatel03/Nimbus/pull/17) | INFRA-1 per-session throwaway test DB (no STATE row) | 2026-10-26 06:27, `c1bc09e` | not recorded | 2 (PR body: round 2 fixes) |
| [#18](https://github.com/devampatel03/Nimbus/pull/18) | P2.3 follow-up: sentence-transformers to optional `local-embed` extra (ADR-0013) | 2026-10-26 00:48, `1ea47ec` | not recorded | not recorded |
| [#19](https://github.com/devampatel03/Nimbus/pull/19) | P2.6 character-card loader, R9 fail-closed guard (ADR-0015) | 2026-10-26 11:03, `d8f078e` | 85.7% → 92.9% | 2 plus pre-merge fixes |
| [#20](https://github.com/devampatel03/Nimbus/pull/20) | P2.2 ingest, Event/Transcript contracts, extractor retyped | 2026-10-26 06:55, `b6f8069` | 92.9% → 92.9% | 2 |
| [#21](https://github.com/devampatel03/Nimbus/pull/21) | P2.3 follow-up: order-independent embedder guard tests, timeouts | 2026-10-26 06:35, `e30361e` | not recorded | not recorded |
| [#22](https://github.com/devampatel03/Nimbus/pull/22) | P2.2 follow-up: ingest recovery, attempts before paid calls, backoff | 2026-10-26 11:12, `b17bb8c` | not recorded (STATE said "in review") | not recorded |
| [#23](https://github.com/devampatel03/Nimbus/pull/23) | P2.4 retrieval: bi-temporal `as_of`/`known_at`, untrusted filters, event-linked chunks; ADR-0017, ADR-0018 | 2026-10-26 16:35, `a44ab06` | 72% → 100% | 2 (two HIGHs fixed: R8 promote binding, R7 known_at) |
| [#24](https://github.com/devampatel03/Nimbus/pull/24) | CI: Postgres service for the redteam job | 2026-10-26 16:33, `6a05471` | not recorded (no STATE row) | not recorded |
| [#25](https://github.com/devampatel03/Nimbus/pull/25) | P2.5b delete Approver, LangGraph, ToolIntent/ToolResult; Tool protocol v2 | 2026-10-26 18:40, `733cbb3` | 92.9% | not recorded |
| [#26](https://github.com/devampatel03/Nimbus/pull/26) | P2.1b app entrypoint (one process, 3 loops), healthcheck, delete `src/api` | 2026-10-27 01:43, `7102180` | 85.7% → 100% | 2 |

Still open at the gate: none of the W2 build tasks. P2.1b (PR #26) closes the W1 R12 gap (log scrubbing installed at the entrypoint). The W1 INV-5 HIGH is closed by PR #25.

## 2. Gate numbers (command output, 2026-10-27)

| Gate | Command | Result | Threshold / target |
|---|---|---|---|
| Unit | `uv run pytest tests/unit -q` | **1090 passed**, 4 skipped | `tests_must_pass: true` — met |
| Integration (real Postgres) | `uv run pytest tests/integration -q` | **151 passed**, 1 skipped | met |
| Invariants | `uv run pytest tests/eval/invariants -q` | **100 passed** | met |
| Non-adversarial (CI test job) | `uv run pytest --ignore=tests/adversarial -q` (with coverage) | **1341 passed**, 5 skipped, 0 failed | met |
| Adversarial | `uv run pytest tests/adversarial -q` | **62 passed** | `redteam_min_cases: 100` — **not met** (P4.7) |
| Lint | `uv run ruff check .` | All checks passed | `lint_clean: true` — met |
| Types | `uv run mypy src` | no issues in **93** source files | `types_clean: true` — met |
| Coverage (`src/llm`, `src/net`, `src/core`, `src/gateway`, `src/ingest`, `src/app`, `src/orchestrator`, `src/memory`) | same run, pytest-cov TOTAL | **96%** (4289 statements, 165 missed). `src/llm` 97.7%, `src/net` 99.2%, `src/core` 100%, `src/gateway` 96.4%, `src/ingest` 97.7%, `src/app` 89.6%, `src/orchestrator` 97.2%, `src/memory` 96.8% | `min_coverage_security_policy: 0.95` — partial (policy, approvals, quarantine not built; `scrub.py` 100%) |
| Changed-line coverage | not computed | not measured | `min_coverage_changed_lines: 0.80` — not measured |
| Gmail send scope | `git grep -E 'gmail\.(send|compose|modify)' -- src config` | 0 hits | `gmail_send_scope_present: false` — met in code. Real scope set is H16 |
| Character card marker | `grep -c 'human-authored: true' personality/character_card.md` | **0** (2 placeholder lines) | `character_card_human_authored_marker_required: true` — **not met** (H1) |
| R9 release gate | `tests/unit/test_r9_character_card.py` | skipped (needs `NIMBUS_REQUIRE_CARD=1`; CI does not set it yet) | final PR stage |
| LoCoMo | `docs/pivot/reports/LOCOMO_REBASELINE_P2.4.md` (stub, not re-run) | stub 13/233 = **5.58%** on 2 of 10 samples | `locomo_min_accuracy_provisional: 0.70` — **NOT MEASURED** (real embedder needs H6 or H3) |
| Provenance (R7), supersession (INV-3), secrets (INV-7) | inside the invariant run | pass | tests pass; no runtime counts |
| Runtime SLOs, ops, cost | — | no live app or VM | not applicable at W2 |

Skips: 3 POSIX permission tests (`tests/unit/app/test_heartbeat.py`), 1 POSIX-signal test (`tests/integration/app/test_entrypoint_process.py`), 1 R9 release gate. The POSIX tests run only on Linux CI.

Test count change: non-adversarial 694 (W1) → 1341 (W2). Adversarial 53 → 62.

## 3. Decisions (this wave)

- **ADR-0016** gateway offset advances on ack; at-least-once; redelivery budget 5. Accepted (P2.1a). The poison-audit wording is still ambiguous (STATE L1, open).
- **ADR-0017** MemoryStore reconciled with TARGET_DESIGN §3. Accepted (P2.4). Records the deliberate deviations: the embedder prefix is not applied, and `assert_fact`, `facts_as_of` and `review_queue` stay in the graph and review modules. Promotion needs a consumed approval (single use, migration `c4e8a1d92b37`). Graph supersede sets the old `valid_to` to `max(new.valid_from, old.valid_from)`.
- **ADR-0018** LoCoMo gate stays provisional (0.70 floor, 0.85 target). Provisional. Thresholds not changed. The real-embedder number is NOT MEASURED.
- **ADR-0006** executed: loop in P2.5a, LangGraph and Approver deleted in P2.5b. Annotated, with the line-27 correction (the two tests were deleted, not rewritten).
- **ADR-0012** executed in P2.1b with two deviations: the heartbeat is a JSON file, not `system_flag` rows, and the HEALTHCHECK_PING_URL ping is not built (P6.4). Annotated.
- **ADR-0014** executed: `Event`/`Transcript` (P2.2), `ToolIntent`/`ToolResult` deleted (P2.5b). `ToolOutput` lives in `src/orchestrator/dispatch.py`. Annotated.
- **ADR-0013** provisional, unchanged. The embedder is an optional `local-embed` extra (PR #18). arm64 latency is not measured (H6).
- **ADR-0015** accepted. The card loader fails closed; the card is still a placeholder (H1).
- Pivot-internal decisions (no ADR): one process with three supervised loops; `APP_ENV` defaults to `production`; `DenyAllWritesPolicy` as the interim policy; a shared `CallAccountant` for `GuardedLLM` and `ApiEmbedder`; Tool protocol v2 (`FunctionTool`, `ToolGroup`); per-session test databases (INFRA-1); the redteam CI job gets Postgres (PR #24).
- ADR-0007 (H16), ADR-0010 (H18) and ADR-0011 (H17) remain Proposed. ADR-0008 and ADR-0009 are Accepted, and their code is not built yet.

## 4. Documentation changes in this PR

- `CLAUDE.md`: rewritten to the W2 state. Process rules, the "Never do" list and the rules for agents are kept. Invariants: INV-5 has its regression test and the legacy-approver violation is closed; the superuser DB role stays HIGH; R8 and R12 are updated; ADR numbering says next: 0019. Size 9,570 → 10,000 bytes (limit about 10 KB; it was 10,009 before the trims in this revision). The diff is in section 11.
- `MEMORY.md`: append-only. New "Pivot W2" section (decisions, gate numbers, lessons, superseded list, open follow-up table with owners, human items). W1 rows marked `[CLOSED W2]` or `[PARTLY CLOSED W2]` in place. Nothing deleted.
- `docs/pivot/reports/KPI.md`: W2 section added on top. The W1 section is kept below as history.
- `docs/decisions/0006`, `0012`, `0014`: `Executed` lines and deviations added (minimal in-place annotations; Decision text unchanged).
- No index file exists in `docs/decisions/`, so none was created.
- Not touched: `quality/thresholds.yaml`, `personality/character_card.md`, code, tests, `docs/pivot/STATE.md`, `docs/pivot/HUMAN_TODO.md`.

## 5. Open risks

1. **No live run.** Telegram, the model provider, Google and Notion are not exercised. The gateway and entrypoint are tested with a fake Telegram transport under the real `EgressClient` and with `FakeLLM`.
2. **INV-5 policy is not real yet.** `DenyAllWritesPolicy` denies every write until P3.1. The INV-5 scanner is a name tripwire.
3. **INV-6 and R8 quarantine are not built.** P3.0 is blocked by H18, and P3.1 and P3.2 depend on P3.0. No code creates promote approvals.
4. **Reliability (PRR inputs, P2.1b).** Reply delivery is at-most-once. A poison update blocks the queue for at least 10 minutes by design. ADR-0016 leaves a residual silent-loss window. The held spend reservation is in-memory and is lost on restart.
5. **Secrets.** Literal secrets shorter than 8 characters are never redacted. The default DB password `nimbus` means production needs a long one. Model output is not canary-checked before it reaches Telegram or history (P4.4).
6. **Superuser app DB role: HIGH (INV-3, R7) until P6.1/P6.2.** Unchanged. There is no waiver. No live run with production credentials until it is waived or fixed (§9, §10).
7. **Embedding and retrieval quality unmeasured.** arm64 latency is NOT MEASURED. The Qwen3 query prefix is not applied. The real LoCoMo baseline is NOT MEASURED, so the 0.70 gate has never been tested with a semantic embedder.
8. **Red-team coverage.** 62 adversarial tests against a 100-case target. `red_team.py` was deleted in P2.5b, and nothing enforces `redteam_min_cases` yet.
9. **Character card.** Still a placeholder. The R9 release gate does not run in CI.
10. **Egress is app-level only.** No nftables or container egress control (P4.5), and no post-DNS private-IP check.
11. **Coverage gaps.** `src/app/cli.py` 84%, `fakes.py` 67%, `heartbeat.py` 89%, `src/memory/schemas/vector.py` 68%.
12. **Process.** PR #26 changed 62 files, above the soft cap of 40. STATE dates drifted from GitHub. INFRA-1 and PR #24 have no STATE rows. Three ADRs carry wording the code no longer matches (ADR-0012 heartbeat, ADR-0014 `ToolOutput` location, ADR-0016 poison audit).
13. **NOTICE** still lists Parakeet-TDT (lines 14-18), pyannote (lines 24-27) and Apache AGE (line 99). None is in the dependency set. An earlier draft of this report and of MEMORY.md said the AGE entry was gone. That was wrong and is corrected. Cleanup is an orchestrator item.
14. **Dependency.** `apscheduler` is still a core dependency (NOTICE line 82). Its removal belongs to P3.3, and the STATE P3.3 row must name it (orchestrator).
15. **Memory write path not wired (gate review MED-1).** No production caller of `record_event`, `add_chunks` or `write_semantic` exists; the only callers are the definitions in `src/memory/manager.py`. Ingest writes only `event` rows (`src/ingest/repository.py:63`). `SessionScopedRecall` (`src/app/recall.py`) reads an empty store. `src/memory/extractor.py` is not imported. The post-turn `memory_extract` job (TARGET_DESIGN §4.1 step 7) has no STATE row. The live bot remembers only the 20-message in-process window (`src/app/pipeline.py`). Owner: P3.3 (job runner, including the record_event → chunk → memory_extract path).
16. **Spend cap not production-ready (gate review MED-3).** The default daily cap is 200 INR with no upper bound (`src/llm/settings.py:58`), against `cost.hard_daily_cap_inr` 100 in `quality/thresholds.yaml`. The `*` price is a placeholder, 250/1000 INR per Mtok (`settings.py:35`). `run_app` builds `GuardedLLM` with no `alert` hook (`src/app/runtime.py:346-352`), so R5 "alerts the user" is not wired. The held reservation is in memory only. Owners: P5.5 (price table, cap), P6.4 (alert).
17. **Kill switch has no operator control.** The switch is the `system_flag` row `llm_kill_switch` (`src/llm/guard.py:73`). `/panic` is not built (only the `KILLED` enum value exists). Engaging it is manual SQL. Owner: unassigned, proposed P6.4.
18. **R4 latent bypass.** `VLLMClient()` with no `guarded=` builds a second `GuardedLLM` on in-memory stores (`src/llm/client.py:93`, `src/llm/factory.py:25`). Its only caller, `check_friction_llm` (`src/personality/agent_character.py:432`), has no caller in `src/`. It is dead code today. Owner: P5.3, or delete the shim.
19. **R10 not wired.** `run_app` passes no stances or traits to `Orchestrator` (`src/app/runtime.py:363-369`). Owner: P5.2 (trait model), P5.3.
20. **Test hygiene (LOW-5).** Two tests assert "directory deleted" on disk, not in git: `tests/unit/app/test_no_fastapi.py:37` and `tests/eval/invariants/test_inv1_single_egress_point.py:473` (protected, so not edited here). A stale `__pycache__` breaks them. Fix: check `git ls-files`. Owner: unassigned.

## 6. Open follow-ups

The full table, with an owner and a source for each row, is in `MEMORY.md` under "Pivot W2" → "Open follow-ups (W2; owner)" (56 rows). It copies the follow-ups from the STATE W2 rows (P2.1a, P2.1b, P2.2, P2.3, P2.4, P2.5a, P2.5b, P2.6) and the PRR inputs. Rows added at this gate are marked "W2 gate" in the Source column: `src/workers` placeholder, PR #26 size, STATE rows for INFRA-1 and PR #24, the stale INV-1 comment, and coverage gaps. INFRA-1 and the PR #24 fix have no STATE rows, so their follow-ups are recorded only here.

## 7. Human items at the gate

- **H1** character card: open. Marker count 0, 2 placeholder lines. Blocks the R9 release gate and the P5.3 evals.
- **H2** Telegram bot token (prod and staging) and allowlisted user ID: open. Blocks the live gateway test (P6.5).
- **H3** model provider key with billing enabled: open. Blocks live LLM calls, an API-embedder LoCoMo run and the P5.4 bake-off.
- **H6** Oracle VM: open. Blocks the arm64 embed latency, the real LoCoMo baseline and P6.x.
- **H12** `.claude/settings.json`: open. The main checkout still shows it modified (the template clobbered the hooks).
- **H16** Gmail scope (ADR-0007): open. Blocks P3.4 live use and P3.6.
- **H17** Q3/Q4/Q5 defaults: defaults applied unless you object.
- **H18** ADR-0010, quarantine every untrusted field: open. Blocks P3.0, and through it P3.1 and P3.2.
- **H19 and H21**: `.env.example` lines and the orphaned Qdrant and Redis containers. By hand. Agents cannot read `.env.*`.
- **H22** CODEOWNERS and branch protection for `personality/**`, the R9 test and `.claude/hooks/**`: open.
- **Waiver (human decision):** the superuser app DB role (HIGH, INV-3/R7) must be waived explicitly or fixed before any live run with production credentials. No waiver is recorded in this PR.
- **Price table and cap (human):** the real provider price table and a daily cap of at most 100 INR before any staging run with a real key.
- **Orchestrator (not human):** NOTICE cleanup; `NIMBUS_REQUIRE_CARD=1` in the CI job for `base=main` (final PR, P7.4); add STATE rows for INFRA-1 and PR #24.
- **Resolved:** H14 (protected-test batch approval, 2026-10-25), H15 (Docker running). H20 is superseded by H14.

## 8. Honest limits

- Telegram is not end-to-end encrypted. Telegram can read messages in plaintext.
- Provider retention applies to the model provider's logs. The W1 figure of up to ~30 days depends on the tier. Not re-verified.
- Oracle can reclaim the VM.
- Prompt injection is bounded, not eliminated. INV-6 is not built, so tool and extractor text is framed in the prompt only.
- "Tests pass" for INV-3, INV-7 and R7 means the current checks pass. It is not a measured leak or violation count.
- No live model or provider was called. No runtime SLO (reply time, reminder delivery, restart, soak) was measured.
- The LoCoMo number is a stub with 2 of 10 samples. It says nothing about retrieval quality with a real embedder.
- Replies are at-most-once. Updates older than Telegram's retention are lost, and the poison budget can drop a legitimate update (ADR-0016).
- Long-term memory is not written by the running app. The bot's memory is the last 20 messages of the current process; nothing is recalled from earlier conversations (§5.15).
- The spend cap is not a production guarantee: default 200 INR, placeholder price, no alert, in-memory reservation (§5.16). Kill switch engagement is manual SQL (§5.17).
- Telegram and the model provider see plaintext. The model output is not canary-checked before it reaches Telegram (P4.4).

## 9. Definition of Done (wave, SDLC §8)

| Item | Status | Evidence |
|---|---|---|
| All W2 task rows merged | MET | PRs #14–#26 merged (GitHub `mergedAt`, §1) |
| Each task: acceptance, tests, gates, reviewer approved | MET, with an exception | Review rounds in §1. P2.2 and P2.4 are merged, but the memory write path (§5.15) was in no W2 row |
| Wave report | MET on merge of this PR | this file |
| KPIs recorded | MET | `docs/pivot/reports/KPI.md`. NOT MET and NOT MEASURED rows are stated, not hidden |
| No open HIGH risk | **NOT MET** | Superuser app DB role (INV-3, R7) until P6.1/P6.2. Needs an explicit human waiver or deferral. Until then: **no live run with production credentials** |
| Checkpoint committed | PENDING | Orchestrator `chore(state)` checkpoint and the W2 rows in STATE |
| Project-level (PRR, UAT, soak) | n/a at W2 | P6.x |

Reviewer verdict on PR #27: APPROVE, 90%, with the doc fixes in this revision (memory path, spend cap, DoD, corrections) applied before merge.

Worst case with real credentials, derived from the code (the reviewer's full text was not available to this author, so this is not a quote):
- The app DB role is a superuser. A bug, or a path that reaches SQL, can edit or delete rows in the append-only tables and disable the triggers (INV-3, R7).
- The spend cap is 200 INR per day with no alert. At the placeholder price, the loss can reach twice the 100 INR threshold before anyone is told.
- Model output is not canary-checked (P4.4), so anything the model writes reaches Telegram unchecked. Telegram and the provider see plaintext in any case (§8).
- `/panic` does not exist. Stopping the spend or the replies means manual SQL on `llm_kill_switch`.

## 10. Next wave (W3): GO with conditions

Gate verdict: **GO with conditions.** Batch dependencies were checked against STATE. The batch order from `docs/pivot/reports/TARGET_DESIGN.md` §9 was not re-verified beyond those dependencies.

1. **Waiver first.** A human waiver of the superuser-role HIGH (INV-3, R7) is required before any live run with production credentials. The fix (P6.1/P6.2) is the alternative.
2. **Start only unblocked work.**
   - P3.3 scheduler and job runner, with the memory write path (`record_event` → chunk → `memory_extract`), the `lane` server default removal (`graph.py:100`, `commitment.py:56`, `decision.py:51`), and the apscheduler removal named on the STATE P3.3 row.
   - P3.4 Google OAuth code and token store. No live credentials until H4.
3. **Blocked.** P3.0, P3.1, P3.2, P3.7, P3.8 and P3.9 wait on H18 (ADR-0010). P3.6 waits on H16. P3.5 needs P3.4 and P3.2, so it waits as well.
4. **Before any staging run with a real key:** set the real price table, set a daily cap of at most 100 INR, and wire the alert hook.

Human items for W3: H18 (security chain), H16 (Gmail), H4 and H5 (live tools). Before the W5 evals: H1 (card), H3 (model key), H6 (VM).

## 11. CLAUDE.md diff (this PR)

The diff against `origin/pivot` for `CLAUDE.md` (generated from the final file):

```diff
diff --git a/CLAUDE.md b/CLAUDE.md
index 5ab683c..dec1584 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -8,57 +8,59 @@ Read `docs/AGENT_OPERATING_MANUAL.md` and `docs/pivot/02_ORCHESTRATION_PROTOCOL.
 
 **Privacy stance (Brief §1, §8):** Telegram, Oracle and the model provider can each see plaintext. We bound leak and injection risk by architecture, not by trusting filters. Never claim more privacy than this.
 
-## Architecture (quick reference, pivot W1 state)
-
-- **Channel:** Telegram bot, long polling, outbound only, allowlisted user ID (R1–R3). Gateway `src/gateway/` is not built yet (P2.1a).
-- **Process:** one service (bot + worker + scheduler), entrypoint P2.1b. Still present until removed: `src/api/` (FastAPI/JWT, ADR-0012) and LangGraph in `src/orchestrator/graph.py` (ADR-0006, removal P2.5b).
-- **Postgres 16 + pgvector:** `infra/docker/postgres/`, `migrations/` (Alembic), `src/db/` (async client, `crypto.py`), ORM in `src/memory/schemas/`. Append-only triggers on fact/event tables (R7, INV-3). Episodic store: `src/memory/store.py` (`memory_chunk`). Jobs: Postgres `SKIP LOCKED` (ADR-0008, not built).
-- **`src/llm` — the only LLM SDK importer (R4, R5).** Guard order: kill switch → scrub (`scrub.py`) → spend cap (R5, reserve-then-bill) → audit row (`llm_audit`) → provider call. Fakes: `src/llm/fake.py`. Settings: `src/llm/settings.py`.
-- **`src/net` — the only network transport importer (R13).** Default-deny host allowlist via the `Service` enum in `src/net/policy.py` (ADR-0004).
-- **`src/core/logging.py`:** `install_log_scrubbing` (INV-7). Module settings live in `src/<module>/settings.py` (`docs/pivot/SETTINGS_PATTERN.md`); `config/settings.py` keeps only Postgres, auth and CORS.
-- **`src/personality/`:** user traits, character-card loader, stance log, friction heuristic. Card: `personality/character_card.md` (ADR-0015).
-- **`src/memory/`:** bi-temporal graph (`graph.py`), `manager.py`, `retriever.py`, `embedder.py` (local Qwen3-Embedding-0.6B, provisional, ADR-0013).
-- **`src/tools/`:** `BaseTool` and the auto-discovery registry (`registry.py` is the only allow-listed dynamic import). Tasks and reminders are local. Calendar, Gmail (drafts only; ADR-0007 Proposed, pending H16) and Drive are rebuilt in W3 on `src/integrations` (ADR-0005).
-- **`src/orchestrator/`:** plain async tool loop with policy and approvals replaces LangGraph and `Approver` (ADR-0006, ADR-0009; not built yet).
-- **`src/contracts/`:** pre-pivot frozen models, retired by ADR-0014; removal P2.2/P2.5b.
-- **Retired, do not reintroduce:** Redis, Qdrant, Apache AGE, local vLLM/Ollama serving, STT/TTS/diarization, Flutter app, `src/security/` and `execute_external` (deleted P1.4c; scrubbing now in `src/llm/scrub.py`), Gmail send, Gemini-as-executor, intent-builder tools.
-- **Tests:** `tests/unit/`, `tests/integration/` (real Postgres), `tests/eval/` (protected, see rule 4), `tests/adversarial/`, `tests/fakes/`, `tests/fixtures/`.
+## Architecture (quick reference, pivot W2 state)
+
+- **Channel:** `src/gateway/` Telegram long polling, outbound only, allowlisted user ID (R1–R3). Offset advances on ack, at-least-once, bounded redelivery; consumer dedupes on `telegram:<update_id>` (ADR-0016).
+- **Process:** `src/app/`, one process, three supervised loops: gateway (`Ingestor.run`, turn queue), worker (`recover_pending`, job seam, no jobs), scheduler (stub). Heartbeat file read by `healthcheck` (ADR-0012). Log scrubbing first (R12). No long-term memory writes yet (`memory_extract` is P3.3).
+- **Postgres 16 + pgvector:** `infra/docker/postgres/`, `migrations/` (Alembic, head `c4e8a1d92b37`), `src/db/`, ORM in `src/memory/schemas/`. Append-only triggers on fact and event tables (R7, INV-3).
+- **`src/llm` — the only LLM SDK importer (R4, R5).** Guard order: kill switch → scrub → spend cap (`CallAccountant`, shared with `ApiEmbedder`) → audit row → provider call.
+- **`src/net` — the only network transport importer (R13).** Default-deny host allowlist, `Service` enum in `src/net/policy.py` (ADR-0004).
+- **`src/core/logging.py`:** `install_log_scrubbing` (INV-7). Module settings: `src/<module>/settings.py` (`docs/pivot/SETTINGS_PATTERN.md`).
+- **`src/personality/`:** every card load goes through `load_approved_character_card` (R9, ADR-0015).
+- **`src/memory/`:** `store.py` episodic chunks (ADR-0017); `graph.py` bi-temporal facts (supersede only); `embedder.py` (local Qwen3, provisional, ADR-0013).
+- **`src/orchestrator/`:** plain async tool loop (`loop.py`, ≤12 tools, caps; ADR-0006). `dispatch.py` holds `DenyAllWritesPolicy`: every write is denied until the real policy (P3.1, ADR-0009).
+- **`src/tools/`:** Tool protocol v2 (`base.py`) and the auto-discovery registry (`registry.py`, the only allow-listed dynamic import). Calendar, Gmail (drafts only; ADR-0007 Proposed, H16) and Drive: W3 (ADR-0005).
+- **Removed in W2, do not reintroduce:** `src/api/`, FastAPI, uvicorn, pyjwt (ADR-0012); LangGraph, `graph.py`, `approver.py` (ADR-0006). Contracts: Event, Transcript, MemoryQuery, UserProfileSlice (ADR-0014).
+- **Retired, do not reintroduce:** Redis, Qdrant, Apache AGE, local vLLM/Ollama, STT/TTS/diarization, Flutter, `src/security/`, `execute_external`, Gmail send, Gemini-as-executor, intent-builder tools.
+- **Tests:** `tests/` unit, integration (real Postgres), `eval/` (protected, rule 4), adversarial, fakes, fixtures.
 
 ## Commands
 
-- `uv sync --extra dev` — always with `--extra dev`; plain `uv sync` removes the dev tools.
-- `docker compose up -d` — starts `postgres` only. Redis, Qdrant and vLLM services are gone.
-- `uv run pytest --ignore=tests/adversarial` — CI test job (needs Postgres up).
-- `uv run pytest tests/adversarial` — CI red-team job.
+- `uv sync --extra dev` — always with `--extra dev`; plain `uv sync` removes the dev tools. `--extra local-embed` only where the local embedder runs.
+- `docker compose up -d` — starts `postgres` only.
+- `uv run python -m src.app` — runs the bot. Needs Telegram and LLM settings from the environment. Fake backends need both `APP_FAKE_BACKENDS=true` and `APP_ENV=development`; anything else is refused.
+- `uv run python -m src.app healthcheck` — exit 0 healthy, 1 unhealthy (stale, missing or malformed heartbeat). `run` exits 2 on startup refused, 3 on embedding migration required, 4 on a fatal loop error.
+- `uv run pytest --ignore=tests/adversarial` (CI test job) and `uv run pytest tests/adversarial` (red team); both need Postgres up. Each session creates and drops its own `nimbus_test_<epoch>_<pid>_<uuid8>` DB (INFRA-1, PR #17); stale ones older than 6 h are dropped at start.
 - `uv run ruff check .` and `uv run mypy src` — CI lint and types.
-- **Deploy:** not built yet (P6.1 arm64 Dockerfile with weights baked in, P6.x runbooks). Never deploy from a task branch.
+- **Deploy:** not built yet (P6.1 arm64 Dockerfile, P6.x runbooks). Never deploy from a task branch.
 
 ## Current phase
 
-**Pivot (Nimbus Lite): W1 foundation gate done (2026-10-25). W2 next**: Telegram gateway and app entrypoint (P2.1a/b), ingest (P2.2), embedder (P2.3), retrieval (P2.4), orchestrator loop (P2.5a/b), character-card loader (P2.6).
-Task table: `docs/pivot/STATE.md`. W1 numbers and open items: `docs/pivot/reports/WAVE_W1.md`.
+**Pivot (Nimbus Lite): W2 built (2026-10-27). Gate pending merge of the gate docs (`docs/pivot/reports/WAVE_W2.md`, GO with conditions).** No live run with production credentials until the superuser-role HIGH below is waived or fixed. W3 next: scheduler and memory write path (P3.3), Google OAuth code (P3.4, live use after H4); lanes (P3.0, blocked on H18) and everything after it wait.
+Task table: `docs/pivot/STATE.md`.
 
 ## Invariants (Brief §4, as implemented; tested in CI, never violate)
 
-- **INV-1 (reframed):** exactly one module calls an LLM (`src/llm`), and only `src/net` imports a network transport. Enforced by `tests/unit/llm/test_llm_import_lint.py` and `tests/eval/invariants/test_inv1_single_egress_point.py` (import scan, fails closed on non-literal dynamic imports; allow-list is `src/net/`→httpx and `src/tools/registry.py`→dynamic-import; `Service.LLM` is referenced only under `src/llm` and `src/net/policy.py`). Not built yet: the runtime zero-transport check (part 4) and the `HF_HUB_OFFLINE=1` assertion (part 5, P2.3).
+- **INV-1 (reframed):** only `src/llm` calls an LLM and only `src/net` imports a transport. Enforced by `tests/unit/llm/test_llm_import_lint.py` and `tests/eval/invariants/test_inv1_single_egress_point.py` (import scan, fails closed on dynamic imports). Zero-transport: `tests/unit/llm/test_llm_guard_r4.py`. `HF_HUB_OFFLINE=1` in `src/memory/embedder.py`.
 - **INV-2: RETIRED** (was: no memory in egress payload / local-only inference). Number never reused.
 - **INV-3:** graph facts are append-only; supersession only. `tests/eval/invariants/test_inv3_graph_facts_immutable.py` (AST scan) plus DB triggers.
 - **INV-4: RETIRED** (was: Mode A never blocks on network; mode A/B removed).
-- **INV-5 (new):** policy decisions are deterministic code; no LLM output grants a permission. Not built yet (P3.1) **Currently violated** by the legacy approver until P2.5b (see Known violations below).
-- **INV-6 (new):** untrusted text enters the trusted lane only as schema-validated, delimited DATA. Not built yet (P3.0; blocked on H18 / ADR-0010).
-- **INV-7 (new):** no secret or token value in logs, prompts, memory, audit rows or error messages. Covered by the log filter, `src/llm/scrub.py` and hidden settings values (`tests/eval/invariants/test_inv7_no_secrets.py`). DB canary sweep and obfuscation cases: P4.4.
+- **INV-5:** no LLM output grants a permission (deterministic policy). Regression: `tests/unit/orchestrator/test_inv5_no_llm_permission.py`. Until P3.1, `DenyAllWritesPolicy` denies all writes. The scanner is a name tripwire.
+- **INV-6:** untrusted text enters the trusted lane only as schema-validated, delimited DATA. Not built (P3.0; blocked on H18 / ADR-0010). Tool output defaults to `untrusted=True`; framing is prompt-only until P3.0.
+- **INV-7:** no secret or token value in logs, prompts, memory, audit rows or errors. Covered by the log filter, `src/llm/scrub.py`, hidden settings and `hide_parameters` (`tests/eval/invariants/test_inv7_no_secrets.py`). Canary sweep: P4.4.
 - **R7 (provenance; was repo INV-5):** every derived memory row has non-null provenance (`tests/eval/invariants/test_r7_memory_provenance.py`).
-- **R8 (untrusted memory never auto-promoted):** not built (P3.0, P4.6).
+- **R8 (untrusted memory never auto-promoted):** enforced for chunk promotion only (consumed approval `tool=memory, action=promote, item_id`, single use, migration `c4e8a1d92b37`). Graph-edge promotion: P4.6. No code creates approvals; quarantine (P3.0) is not built.
+- **R12 (no secrets in logs):** met at the entrypoint: `src/app/runtime.py` calls `install_log_scrubbing(cfg.secrets)` before anything else logs (P2.1b).
 - **Known violations, with severity:**
-  - **HIGH, until W2 (INV-5):** legacy `src/orchestrator/approver.py` `_llm_judge` (lines 162-193) returns `approved` taken from model output. It is reached from `src/orchestrator/graph.py:220` (`review_async`) and built through `GraphDependencies` in `src/api/main.py`. No entrypoint runs it yet. Interim fix: `DenyAllWritesPolicy` (P2.5a). Removal: P2.5b. Real policy: P3.1.
-  - **HIGH, until P6.1/P6.2 (INV-3, R7):** the app DB role `nimbus` is a superuser and can disable the append-only triggers. Fix: non-superuser app role.
+  - **HIGH, until P6.1/P6.2 (INV-3, R7):** the app DB role `nimbus` is a superuser and can disable the append-only triggers. Fix: non-superuser app role. Waiver needed before any live run with production credentials.
+  - **Open, not HIGH (WAVE_W2 §5):** spend cap default 200 INR, no upper bound (threshold 100); `*` price is a placeholder; no alert hook; kill switch is a `system_flag` row, no `/panic`. Before any staging run with a real key: real price table and cap ≤ 100 INR.
 
 ## Conventions
 
 - **TDD:** failing test first. For security guards, prove the test can fail (break the code, or monkeypatch in-test). Never leave a weakened security file on disk.
-- **Decisions:** architecture changes get an ADR in `docs/decisions/` (next: 0016) and a dated MEMORY.md entry. Thresholds in `quality/thresholds.yaml` change only via ADR plus reviewer sign-off.
+- **Decisions:** architecture changes get an ADR in `docs/decisions/` (next: 0019) and a dated MEMORY.md entry. Thresholds in `quality/thresholds.yaml` change only via ADR plus reviewer sign-off.
 - **Git:** task branches `task/<id>-<slug>` off `pivot`; PRs target `pivot`. Stage explicit paths only (`git add <path>`), never `git add -A`.
-- **Windows:** the working tree is CRLF (`core.autocrlf`); check `git diff --stat` before committing. Commit hooks (mypy env build) are slow and memory-heavy here; do not run them in parallel.
+- **Windows:** working tree is CRLF (`core.autocrlf`); check `git diff --stat` before committing. Do not run commit hooks in parallel.
 
 ## Never do
 
@@ -75,8 +77,8 @@ Task table: `docs/pivot/STATE.md`. W1 numbers and open items: `docs/pivot/report
 ## Task tracking
 
 - Live table: `docs/pivot/STATE.md` (orchestrator-owned).
-- Pre-pivot T-rows (T0.1–T4.x, Step 11 and the "this session" rows) are SUPERSEDED by the pivot task table. Full text: `git show b9fb56d:CLAUDE.md`.
-- Pivot status: P0 (PR #1), P1.0 (#4), P1.1 (#3), P1.2a (#2, arm64 not run), P1.2b (#6), P1.2c (#7), P1.3a (#9), P1.3b (#10), P1.4a (#5), P1.4b (#8), P1.4c (#11), P1.5 (#12). W1 gate: see `docs/pivot/reports/WAVE_W1.md`.
+- Pre-pivot T-rows are SUPERSEDED by the pivot task table (`git show b9fb56d:CLAUDE.md`).
+- Pivot PRs: W1 #1–#13, W2 #14–#27 (`docs/pivot/reports/WAVE_W*.md`).
 
 ## Rules for agents
 
```
