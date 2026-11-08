# WAVE W4 report — security batch (exfil, canaries, memory poisoning, egress) and W5 progress, gate

Date: 2026-11-08. Branch `task/W4-gate-docs`, worktree `C:/DEVAM/MY_PROJECTS/Nimbus-W4gate`, from `pivot` @ `e5d7780`. Docs-only PR into `pivot`. No code, no tests run (machine memory constrained), no `tests/eval/**`, no thresholds, no character card.
Numbers are copied from PR bodies (named per row) or from `docs/pivot/reports/KPI.md`. Nothing here was re-measured at this gate. Merge times are UTC from GitHub `mergedAt`; SHAs from `git log origin/pivot`.

## 1. What shipped (PRs into `pivot`)

| PR | Task | Merged (UTC), SHA | Result (from PR body) |
|---|---|---|---|
| [#46](https://github.com/devampatel03/Nimbus/pull/46) | P4.3 exfil filter: image/URL stripping, previews off, reply URL allowlist | 2026-10-29 16:10, `9c03a0b` | 84-case corpus; filter disabled makes 93 of 95 adversarial cases fail |
| [#47](https://github.com/devampatel03/Nimbus/pull/47) | P4.4 canary tripwires, outbound blocking, alert, DB sweep (ADR-0023) | 2026-10-29 20:53, `3b1ea3f` | 66 adversarial cases; 14 of 14 mutants killed after review |
| [#48](https://github.com/devampatel03/Nimbus/pull/48) | P4.6 memory poisoning controls (ADR-0024; migration `e5b1c7a3d902`) | 2026-11-01 14:35, `bac363c` | promotion trigger on `memory_chunk`/`graph_edge`; `ReviewQueue` |
| [#49](https://github.com/devampatel03/Nimbus/pull/49) | P4.5 container egress: internal compose net, nftables backstop, post-DNS address check | 2026-11-01 17:03, `4329dd9` | nftables file **UNTESTED** (never loaded) |
| [#50](https://github.com/devampatel03/Nimbus/pull/50) | P4.3 follow-up: CJK host fold, Reject race, canary estimate, ADR and HUMAN_TODO docs | 2026-11-01 18:02, `5538f29` | canary false-hit about 2e-7 per 4 KB (0 of 1500 measured) |
| [#51](https://github.com/devampatel03/Nimbus/pull/51) | P4.1 quarantine-lane red team; drop model-supplied `_target` on approval cards | 2026-11-03 07:44, `cdd1ea7` | 19 vectors x 8 entry paths = 152 end-to-end cases; 268 tests |
| [#52](https://github.com/devampatel03/Nimbus/pull/52) | P4.1 follow-up: tool-output trust contract, lying-tool vector, name-independent email guard, flaky test fix | 2026-11-03 13:34, `195a724` | external-service output always untrusted; full suite not finished (memory stop) |
| [#53](https://github.com/devampatel03/Nimbus/pull/53) | P5.5 cost-minimal provider config, request limiter, 429 yield, cost model (ADR-0025) | 2026-11-03 13:34, `262ca25` | cap default 50 INR, max 100; cheap model; limiter at guard step 3b |
| [#55](https://github.com/devampatel03/Nimbus/pull/55) | W5 follow-ups to #52/#53: non-vacuous taint-carry test, stronger email scans, free-tier startup warning, first-party price sources, `ENV_CHECKLIST.md` | 2026-11-03 14:07, `4a46f88` | carry-off control runs for all 5 categories |
| [#54](https://github.com/devampatel03/Nimbus/pull/54) | P5.1 proactive jobs: brief, check-in, nudges, weekly review (template-only, zero model calls; ADR-0011 addendum) | 2026-11-08 00:04, `e5d7780` | budget ≤6/day, quiet hours 22:00–08:00 IST; mutation-proved |

Decisions recorded: ADR-0023 (canary tripwires), ADR-0024 (memory poisoning controls), ADR-0025 (cost-minimal provider config) are all **Proposed** and await sign-off (H33). ADR-0011 has a P5.1 addendum. Next ADR number: **0026**.

Not shipped in W4/W5: **P4.2** (no such task; numbering gap). **P4.7** red-team corpus (protected `tests/eval`; needs a new human approval). **P4.8** security review (waits P4.7). **P5.2** trait model and "forget that about me" veto (builder stopped by human; offered again). **P3.6** Gmail (H16). **P5.3, P5.4** need H3 keys.

## 2. Gate numbers

Copied from PR bodies, see `docs/pivot/reports/KPI.md` section "W4 gate". No command was run at this gate.

| Gate | Value | Source | Threshold | Status |
|---|---|---|---|---|
| Non-adversarial suite, latest full run | 3804 passed, 1 failed | #53 | `tests_must_pass` | known red: `hung_gateway_poll` fails on baseline |
| Adversarial pytest items, latest | 1268 passed, 1 skipped | #52 | `redteam_min_cases` 100 | counts items, **not** a corpus; P4.7 not built |
| Lint, types | ruff clean, mypy clean | #52, #53 | `lint_clean`, `types_clean` | met at those heads |
| Coverage | not measured | n/a | `min_coverage_security_policy` 0.95 | not measured |
| LoCoMo | not measured | ADR-0018 | 0.70 / 0.85 | not measured |
| Cost, cheap profile gpt-5-nano | about 63 INR/month at 100 msgs/day | #53, `COST_MODEL.md` | `cost.projected_monthly_inr_max` 1500 | **assumption**, not measured; placeholder price gives about 5,837 INR/month |

## 3. Decisions (this wave)

- Exfil and canary controls are **tripwires, not boundaries** (ADR-0023, PR #47 "Limits"). Injection is bounded by code paths, not eliminated.
- A promotion needs a consumed `memory`/`promote` approval at the DB layer (trigger `nimbus_promotion_guard`, ADR-0024). A superuser role can still disable it (HIGH, see section 6).
- External-service tool output is always untrusted, whatever its flag says (#52). Accepted UX cost: a `tasks_*`/`reminders_*` write in the same turn as, or the carry window after, a Calendar/Drive/Notion read needs an approval card (#55).
- Proactive messages are templates with zero model calls. A model opener needs an INV-6 allow-list entry in the protected suite plus a human decision (ADR-0011 addendum).
- Spend cap: default 50 INR, max 100 (ADR-0025). Free-tier Gemini content may be used by Google to improve products; use a free key only with test data unless H3 accepts it (#53).
- Protected-test promotions wait on humans: canary wiring test (H31); lane default (H23); disabled-tool verdict (H29).

## 4. Invariant and requirement coverage

| ID | Mechanism (PR) | Test or evidence | Honest limit |
|---|---|---|---|
| INV-5 | `PolicyEngine` deterministic (#31); approvals single-use, user-only, TOTP (#35); forged and other-user taps do nothing (#51) | `tests/unit/orchestrator/test_inv5_no_llm_permission.py`; 152 red-team cases | scanner is a name tripwire; approvals off without env keys (H24) |
| INV-6 | Lanes and DATA framing (#29, #43); external output untrusted (#52); quarantine budget 10/turn (#43); taint carry (#55) | `tests/adversarial/quarantine_redteam` (268 tests), `tests/adversarial/lanes/` | bounded, not eliminated; interim quarantine-all until H18; framing is prompt-level for the main model; email category deferred (H16) |
| INV-7 | Canary screens on reply, tool args, Telegram, every `EgressClient.request`, logs (#47); `canary-sweep` (counts only); scrub (R12) | canary unit about 200 cases; 66 adversarial; 14 of 14 mutants | tripwire; pieces under 6 chars and model-invented ciphers not caught; sweep cannot read encrypted columns; wiring test is H31-protected |
| R7 | Promotion guard trigger (#48); provenance tests | `tests/eval/invariants/test_r7_memory_provenance.py` | superuser role can disable triggers (HIGH) |
| R8 | `ReviewQueue`, one card per item, no bulk approve (#48); reject retracts via `invalidated_at` | promotion adversarial tests (#48); approval end-to-end (#51) | `decision` and legacy `commitment` are CHECK-only (no writer guard) |
| R13 | Default-deny `src/net` (ADR-0004); post-DNS address check (#49); Notion narrowed paths (ADR-0021); cheap model on same host (#53); Gmail scope/host AST guard (#52) | `tests/unit/net/`, compose and nft lint tests | nftables **UNTESTED**; compose internal net not run; check-then-connect DNS race |
| Exfil | Strip images, links, IPs, domains; post-check; previews off; reply URL allowlist (#46, #50) | 84-case corpus; mutation proofs | `@mentions`, hashtags, commands left alone; NFKC changes look; filenames like `main.py` defanged (fail closed); CJK fold LOW false-positive class (#50) |
| Egress layers | (1) app allowlist `src/net/policy.py`; (2) canary check before every egress request (#47); (3) post-DNS address block (#49); (4) compose internal network (#49); (5) nftables host backstop (#49) | per-layer tests as listed above | layers 4 and 5 are config, not run; layer 3 is check-then-connect |

## 5. Honest limits (W4/W5)

- **Tripwires, not boundaries:** canary and exfil filters catch known shapes. A new encoding or a split piece can pass.
- **Host layer unrun:** nftables file never loaded (PR #49 says so); internal compose network not run on a host. Do not claim the egress boundary holds at the host level.
- **DNS race:** the post-DNS check runs after resolution and before connect. It does not stop a resolver answering differently at connect time.
- **CJK fold LOW:** a CJK run followed by `:` and an ASCII URL character is folded as a host (PR #50 rule). Prose can be removed. A fully CJK host with no ASCII delimiter is kept as text (documented limit).
- **Approval-card links:** URLs in `<pre>` payloads are assumed non-tappable on Android, iOS and Desktop. Not checked live (H30). Interim: fixed warning line for non-allowlisted addresses.
- **Known red test:** `test_hung_gateway_poll_makes_the_heartbeat_go_stale_while_others_stay_fresh` fails on pivot baseline (Windows, missing `hb.json`); not caused by W4/W5 PRs.
- **Telegram is not E2EE; provider retention; Oracle reclaim** (Brief §8) are unchanged. Free-tier provider use may retain content (H3).
- **Costs are assumptions** (#53); no provider call was measured.

## 6. Open risks

1. **HIGH:** app DB role `nimbus` is a superuser and can disable append-only triggers (INV-3, R7, promotion guard). No waiver recorded. Fix in P6.1 (non-superuser role). Blocks any live run with production credentials.
2. Spend cap and prices: placeholder `*` price in env until H3; no alert hook; kill switch is a `system_flag` row (no `/panic`).
3. P4.7 corpus not built; "100 red-team cases" is not yet true (pytest items are not cases).
4. Protected-test edits pending (H23, H29, H31). Three ADRs (0023–0025) and four older ones await sign-off (H33).
5. Full suite is about 28 min and memory-heavy: #52's full run was stopped at about 15 percent.

## 7. Human items at the W4 gate

- Protected-test approvals: H23 (lane default), H29 (disabled-tool verdict), H31 (canary wiring test into `tests/eval`). P4.7 needs a new approval.
- ADR sign-offs (H33): 0020 (H32), 0021 (H26), 0022 (H27), 0023, 0024, 0025, 0007 (H16), 0010 (H18).
- Env names by hand in `.env.example` (agents do not read `.env.*`): `CANARY_SEED`, `CANARY_ALERT_TELEGRAM`, `CANARY_ALERT_COOLDOWN_S`, `TELEGRAM_REPLY_URL_ALLOWLIST`, `APPROVAL_*`, `GOOGLE_*`, `NOTION_TOKEN` (H24, H28, H31).
- H3: billing key, provider budget alert, real price table, free-tier privacy decision, and verify the gpt-5-nano row against the first-party OpenAI pricing page (`COST_MODEL.md` cites it; the canonical URL returned HTTP 403 to automated fetches).
- H30: live check that approval-card URLs in `<pre>` are not tappable (Android, iOS, Desktop). Needs H2.
- P5.2: go or no-go on the trait model and "forget that about me" veto.

## 8. W4 gate verdict and next wave

- **W5 (non-live build): GO.** Conditions: P5.4 bake-off waits on H3 keys; P4.7 corpus waits on approval; no claim that the 100-case red-team corpus exists until P4.7 lands.
- **W6 (deploy, first run): NO-GO until** all of: (1) non-superuser app role in P6.1, or a written waiver; (2) nftables file loaded and tested on the VM (P4.5 layer 5); (3) compose internal network run on a host (P4.5 layer 4); (4) H30 checked; (5) real price table and cap at or below 100 INR before any real key; (6) the known red test explained or fixed on Linux CI; (7) first-run readiness (H2, H6–H9).
- **Next wave:** finish W5 (P5.4 when H3 keys exist, P5.3, P5.5 LoCoMo gate), then W6 (P6.1 arm64 image, `deploy/`, non-superuser role, first-run readiness). P4.7 and P4.8 run when approved.

## 9. CLAUDE.md diff

The diff below is the `CLAUDE.md` change in this PR. Size: 10705 bytes before, 11166 after (+461, under the +600 budget).

```diff
@@ -8,21 +8,21 @@ Read `docs/AGENT_OPERATING_MANUAL.md` and `docs/pivot/02_ORCHESTRATION_PROTOCOL.
 
 **Privacy stance (Brief §1, §8):** Telegram, Oracle and the model provider can each see plaintext. We bound leak and injection risk by architecture, not by trusting filters. Never claim more privacy than this.
 
-## Architecture (quick reference, pivot W3 state)
+## Architecture (quick reference, pivot W5 state)
 
-- **Channel:** `src/gateway/` Telegram long polling, outbound only, allowlisted user ID (R1–R3). Offset advances on ack, at-least-once, bounded redelivery; consumer dedupes on `telegram:<update_id>` (ADR-0016).
-- **Process:** `src/app/`, one process, three supervised loops: gateway (`Ingestor.run`, turn queue), worker (job runner: reminders and `memory_extract`, plus `recover_pending`), scheduler (`MemoryExtractSweeper` backstop). Heartbeat file read by `healthcheck` (ADR-0012). Log scrubbing first (R12). Google/Notion groups bind only with credentials (`src/app/integrations.py`).
-- **Postgres 16 + pgvector:** `infra/docker/postgres/`, `migrations/` (Alembic, head `a9d4c6e2f801`), `src/db/`, ORM in `src/memory/schemas/`. Append-only triggers on fact, event, `policy_audit` and `commitment_ledger` tables (R7, INV-3). The app role is a superuser (HIGH, see Known violations).
-- **`src/llm` — the only LLM SDK importer (R4, R5).** Guard order: kill switch → scrub → spend cap (`CallAccountant`, shared with `ApiEmbedder`) → audit row → provider call.
-- **`src/net` — the only network transport importer (R13).** Default-deny host allowlist, `Service` enum in `src/net/policy.py` (ADR-0004).
+- **Channel:** `src/gateway/` Telegram long polling, outbound only, allowlisted user ID (R1–R3). At-least-once, bounded redelivery; consumer dedupes on `telegram:<update_id>` (ADR-0016). Exfil filter on sends (P4.3).
+- **Process:** `src/app/`, one process, three supervised loops: gateway (`Ingestor.run`, turn queue), worker (job runner: reminders and `memory_extract`, plus `recover_pending`), scheduler (`MemoryExtractSweeper`; proactive jobs, P5.1, no model call). Heartbeat file read by `healthcheck` (ADR-0012). Log scrubbing first (R12). Google/Notion groups bind only with credentials (`src/app/integrations.py`).
+- **Postgres 16 + pgvector:** `infra/docker/postgres/`, `migrations/` (Alembic, head `e5b1c7a3d902`), `src/db/`, ORM in `src/memory/schemas/`. Append-only triggers on fact, event, `policy_audit` and `commitment_ledger` tables (R7, INV-3); promotion guard `nimbus_promotion_guard` on `memory_chunk`/`graph_edge` (R8, P4.6). The app role is a superuser (HIGH, see Known violations).
+- **`src/llm` — the only LLM SDK importer (R4, R5).** Guard order: kill switch → lane → scrub → rate limiter → spend cap (`CallAccountant`, shared with `ApiEmbedder`) → audit row → provider call.
+- **`src/net` — the only network transport importer (R13).** Default-deny host allowlist, `Service` enum in `src/net/policy.py` (ADR-0004). Post-DNS address check (P4.5).
 - **`src/core/logging.py`:** `install_log_scrubbing` (INV-7). Module settings: `src/<module>/settings.py` (`docs/pivot/SETTINGS_PATTERN.md`).
 - **`src/personality/`:** every card load goes through `load_approved_character_card` (R9, ADR-0015).
-- **`src/memory/`:** `store.py` episodic chunks (ADR-0017); `graph.py` bi-temporal facts (supersede only); `commitments.py` commitment ledger (ADR-0022, Proposed); `embedder.py` (local Qwen3, provisional, ADR-0013).
-- **`src/policy/`, `src/approvals/`, `src/lanes/`, `src/orchestrator/`:** table-driven `PolicyEngine` (ADR-0009) wired into dispatch, creates denied (`PENDING_IDEMPOTENCY`), decisions in `policy_audit`. HMAC single-use approvals (ADR-0020, Proposed; off without H24). Untrusted text reaches the model only as DATA via `Quarantine.extract` (budget 10 per turn). Loop ≤12 tools (ADR-0006).
-- **`src/tools/`:** Tool protocol v2 and registry (`registry.py`). Gmail not built (P3.6, H16, ADR-0007 Proposed).
+- **`src/memory/`:** `store.py` episodic chunks (ADR-0017); `graph.py` bi-temporal facts (supersede only); `commitments.py` commitment ledger (ADR-0022, Proposed); `review.py` ReviewQueue (P4.6); `embedder.py` (local Qwen3, provisional, ADR-0013).
+- **`src/policy/`, `src/approvals/`, `src/lanes/`, `src/orchestrator/`:** table-driven `PolicyEngine` (ADR-0009) wired into dispatch, creates denied (`PENDING_IDEMPOTENCY`), decisions in `policy_audit`. HMAC single-use approvals (ADR-0020, Proposed; H32). Canary tripwires (`src/canary/`, ADR-0023) screen replies, args, sends, egress, logs. Untrusted text reaches the model only as DATA via `Quarantine.extract` (budget 10 per turn). Loop ≤12 tools (ADR-0006).
+- **`src/tools/`:** Tool protocol v2 and registry (`registry.py`). Calendar, Drive, Notion built; Gmail not built (P3.6, H16, ADR-0007 Proposed). External-service output is untrusted.
 - **Removed in W2, do not reintroduce:** `src/api/`, FastAPI, uvicorn, pyjwt (ADR-0012); LangGraph, `graph.py`, `approver.py` (ADR-0006). Contracts: Event, Transcript, MemoryQuery, UserProfileSlice (ADR-0014).
 - **Retired, do not reintroduce:** Redis, Qdrant, Apache AGE, local vLLM/Ollama, STT/TTS/diarization, Flutter, `src/security/`, `execute_external`, Gmail send, Gemini-as-executor, intent-builder tools.
-- **Tests:** `tests/` unit, integration (real Postgres), `eval/` (protected, rule 4), adversarial, fakes, fixtures.
+- **Tests:** `tests/` unit, integration (real Postgres), `eval/` (protected, rule 4), `tests/adversarial/` (red team), fakes, fixtures.
 
 ## Commands
 
@@ -36,7 +36,7 @@ Read `docs/AGENT_OPERATING_MANUAL.md` and `docs/pivot/02_ORCHESTRATION_PROTOCOL.
 
 ## Current phase
 
-**Pivot (Nimbus Lite): W3 built (2026-10-29), gate PR open (`docs/pivot/reports/WAVE_W3.md`).** Merged: P3.0–P3.5, P3.7–P3.9 (PRs #28–#44). Nothing ran live. No live run with production credentials until the superuser-role HIGH is waived or fixed. Open: P3.6 (H16), H23, cost gap. Next: W4 security batch (P4.1, P4.3–P4.6).
+**Pivot (Nimbus Lite): W4 gate docs open (`docs/pivot/reports/WAVE_W4.md`); W5 in progress.** Merged: P4.1, P4.3–P4.6, P5.1, P5.5 groundwork (PRs #46–#55). Not built: P4.7 (protected corpus, needs approval), P5.2, P3.6 (H16). Nothing ran live. No live run with production credentials until the superuser-role HIGH is fixed (P6.1) or waived.
 Task table: `docs/pivot/STATE.md`.
 
 ## Invariants (Brief §4, as implemented; tested in CI, never violate)
@@ -47,19 +47,19 @@ Task table: `docs/pivot/STATE.md`.
 - **INV-4: RETIRED** (was: Mode A never blocks on network; mode A/B removed).
 - **INV-5:** no LLM output grants a permission. `PolicyEngine` decides (P3.1); `user_confirmed` comes only from a sealed `ConsumedApproval` (P3.2, ADR-0020). Tests: `tests/eval/invariants/test_inv5_*.py`, `tests/unit/orchestrator/test_inv5_no_llm_permission.py`.
 - **INV-6 (built, PRs #29, #43):** untrusted text enters the trusted lane only as schema-validated DATA; Calendar and Notion give the model opaque handles only. Tests: `tests/eval/invariants/test_inv6_*.py`, `tests/adversarial/lanes/`. Interim: quarantine every untrusted field (ADR-0010, H18). Lane default `'trusted'` stays on three tables (H23).
-- **INV-7:** no secret or token value in logs, prompts, memory, audit rows or errors. Covered by the log filter, `src/llm/scrub.py`, hidden settings and `hide_parameters` (`tests/eval/invariants/test_inv7_no_secrets.py`). Canary sweep: P4.4.
+- **INV-7:** no secret or token value in logs, prompts, memory, audit rows or errors. Covered by the log filter, `src/llm/scrub.py`, hidden settings and `hide_parameters` (`tests/eval/invariants/test_inv7_no_secrets.py`). Canary tripwires and `canary-sweep` (ADR-0023): not boundaries.
 - **R7 (provenance; was repo INV-5):** every derived memory row has non-null provenance (`tests/eval/invariants/test_r7_memory_provenance.py`).
-- **R8 (untrusted memory never auto-promoted):** chunk promotion needs a consumed approval (migration `c4e8a1d92b37`). Confirmed commitments need a consumed `commitment`/`confirm` approval (trigger `nimbus_ledger_guard`, ADR-0022). Graph-edge promotion: P4.6.
+- **R8 (untrusted memory never auto-promoted):** chunk promotion needs a consumed approval (migration `c4e8a1d92b37`). Confirmed commitments need a consumed `commitment`/`confirm` approval (trigger `nimbus_ledger_guard`, ADR-0022). Graph-edge promotion: same rule (trigger `nimbus_promotion_guard`, ADR-0024).
 - **R12 (no secrets in logs):** met at the entrypoint: `src/app/runtime.py` calls `install_log_scrubbing(cfg.secrets)` before anything else logs (P2.1b).
 - **Known violations, with severity:**
   - **HIGH, until P6.1/P6.2 (INV-3, R7):** the app DB role `nimbus` is a superuser and can disable the append-only triggers. Fix: non-superuser app role. Waiver needed before any live run with production credentials.
-  - **Open, staging-blocking (WAVE_W3 §5):** projected chat cost ~4,400–5,550 INR/mo at placeholder prices vs `cost.projected_monthly_inr_max` 1500 (`quality/thresholds.yaml`).
+  - **Open, staging-blocking (WAVE_W3 §5):** projected cost over `cost.projected_monthly_inr_max` 1500 at placeholder prices (WAVE_W3 §5; H3).
   - **Open, not HIGH (WAVE_W2 §5):** spend cap default 50 INR, max 100 (ADR-0025 Proposed); `*` price is a placeholder; no alert hook; kill switch is a `system_flag` row, no `/panic`. Before any staging run with a real key: real model price (`LLM_PRICE_*`).
 
 ## Conventions
 
 - **TDD:** failing test first. For security guards, prove the test can fail (break the code, or monkeypatch in-test). Never leave a weakened security file on disk.
-- **Decisions:** architecture changes get an ADR in `docs/decisions/` (next: 0023) and a dated MEMORY.md entry. Thresholds in `quality/thresholds.yaml` change only via ADR plus reviewer sign-off.
+- **Decisions:** architecture changes get an ADR in `docs/decisions/` (next: 0026) and a dated MEMORY.md entry. Thresholds in `quality/thresholds.yaml` change only via ADR plus reviewer sign-off.
 - **Git:** task branches `task/<id>-<slug>` off `pivot`; PRs target `pivot`. Stage explicit paths only (`git add <path>`), never `git add -A`.
 - **Windows:** working tree is CRLF (`core.autocrlf`); check `git diff --stat` before committing. Do not run commit hooks in parallel.
 
@@ -79,7 +79,7 @@ Task table: `docs/pivot/STATE.md`.
 
 - Live table: `docs/pivot/STATE.md` (orchestrator-owned).
 - Pre-pivot T-rows are SUPERSEDED by the pivot task table (`git show b9fb56d:CLAUDE.md`).
-- Pivot PRs: W1 #1–#13, W2 #14–#27, W3 #28–#44 (`docs/pivot/reports/WAVE_W*.md`).
+- Pivot PRs: W1 #1–#13, W2 #14–#27, W3 #28–#45, W4–W5 #46–#55 (`docs/pivot/reports/WAVE_W*.md`).
 
 ## Rules for agents
 
```
