# PIVOT STATE (orchestrator-owned; commit on every checkpoint)

```yaml
status: RUNNING                # NOT_STARTED | RUNNING | BLOCKED | DONE
wave: W1
mode: balanced                 # economy | balanced | turbo
concurrency_cap: 3
integration_branch: pivot
final_merge_to_main: human     # human | auto
ci: github                     # github | local-only | unknown
last_checkpoint_utc: null
started_utc: 2026-10-24T15:16Z
last_gate_numbers: {}
```

## Task table
Refined by P0.5 from `reports/TARGET_DESIGN.md` §9 (file-ownership globs, `PROT`/`cf` flags and parallel batches live there; ADRs in `docs/decisions/`). `PROT` tasks edit protected `tests/eval/**` and wait on H14.

| ID | Title | Agent | Deps | Status | Branch | PR | Notes |
|----|-------|-------|------|--------|--------|----|-------|
| P0.1–P0.3, P0.6 | Audit, coupling, envcheck | repo-auditor/devops | – | merged | | | done |
| P0.4/P0.5 | Target design + task table | architect | P0.3 | merged | | | done (this file) |
| P1.0 | Delete dead weight (M0) | backend | – | merged | | | pure deletion | PR #4 (8/8). `.env.example` VLLM line -> H19; NOTICE still lists arq -> P1.5.
| P1.1 | CI on pivot: ruff/mypy/pytest + pgvector svc + red-team job + secret scan | devops | – | merged | | |  | PR #3 (11/12). CI green on pivot; qdrant:latest unpinned until P1.3; secret-scan uses detect-secrets-hook (no unaudited-baseline flag).
| P1.2a | Postgres image AGE→pgvector | devops | – | merged-partial | | | PR #2 (85%). Static only: docker build/CREATE EXTENSION/arm64 buildx NOT MEASURED (H15). Run reviewer's commands when Docker is up; `docker compose down -v` first. Pin tag digest in P6.1. Stale 'Apache AGE' comment config/settings.py:17 -> fix in P1.5 | UPDATE 2026-10-25: amd64 build + `CREATE EXTENSION vector` + `SELECT '[1,2,3]'::vector` verified by orchestrator; arm64 buildx build succeeded (image exists), NOT run under emulation.
| P1.2b | Alembic baseline: personality tables revision + upgrade==ORM test | backend | P1.2a | merged | | |  | PR #6 (11/11). Alembic head a1c3f5e7b9d2; new ORM models must be imported in migrations/env.py. LOW for P1.2c: compare_metadata ignores CHECK constraints/server defaults (add pg_constraint check or compare_server_default); ORM defaults are Python-only.
| P1.2c | Pivot schema: event/memory_chunk/job/llm_audit/system_flag/pending_action/inbound_update; provenance cols; append-only triggers; db/crypto.py | backend+memory | P1.2b | merged | | | PROT | PR #7 round 1 = 87.5%: fixing TRUNCATE trigger, FK promoted_approval_id->pending_action, stale skipif (agent resumed). Follow-ups: drop legacy lane='trusted' server default when P2.x touches memory writers; app DB role `nimbus` is superuser (can disable triggers / session_replication_role=replica) -> non-superuser app role in P6.1/P6.2 (DevOps); jsonb numeric-scale rewrite is a trigger no-op gap (LOW); source_event_id on legacy tables immutable (backfill needs trigger-drop migration); ADR-0001/0014 wording amendments at wave gate. MERGED PR #7 (round 2: 16/16; 555 passed 1 skipped). LOW open: R8 FK only proves the approval row exists, not that it is consumed -> enforce in P3.1/P3.2 (policy/approvals) or a trigger; key decoder accepts mixed std/urlsafe base64 + edge whitespace.
| P1.3a | Redis out (M5) | memory | P1.2c | merged | | | PROT, cf | PR #9 merged (12/12, 1af79e0). Orchestrator already dropped redis from pyproject/lock/compose/CI/NOTICE/pre-commit on the branch.
| P1.3b | pgvector MemoryStore episodic + Qdrant out (M6) | memory | P1.3a | merged | | | PROT, cf | PR #10 merged 2026-10-25 (08d1d4f, 13/14 = 92.9%). LoCoMo stub baseline before=after: 99/1540 = 6.4286% (docs/pivot/reports/LOCOMO_BASELINE_STUB.md); harness wall time 51s -> ~8min (per-sample scratch DB; batch inserts follow-up). FOLLOW-UPS: MED-1 store creates its own synthetic event per chunk -> P2.2/P2.x ingest must pass the real event_id (record_event idempotent on source_key; chunk_index); MED-2 ChunkHit must carry lane/untrusted before any promote path (INV-6); reconcile shipped MemoryStore protocol (store.py) with TARGET_DESIGN protocols.py (record_event/add_chunks/promote) in P2.x or ADR; Qwen3Embedder needs model_id attr (P2.3); invalidate on unknown id silently no-ops (LOW); hnsw.iterative_scan SET LOCAL leaks to rest of txn + session filter unindexed (LOW); harness _scratch_dsn breaks on DSN query strings. MEMORY.md/CLAUDE.md still mention Qdrant (12 refs, AD-21 obsolete) -> W1 gate. .env.example QDRANT_* -> P1.5/H19.
| P1.4a | `src/net` egress client + R13 test | security | P1.1 | merged | | |  Branch task/P1.4a-net-egress pushed (b44f6eb), NO PR: code done (408 unit pass, ruff/mypy clean, src/net 99% cov) but protected INV-1 test fails (6 httpx sites; needs src/net/http.py allow-listed) -> needs H14. Orchestrator-side follow-ups: .env.example NET_* vars, httpx in pre-commit mypy deps, ActionSpec no-URL rule in P2.5b; MED: no size cap in build_sdk_http_client, no post-DNS private-IP check (P4.5). PR #5 (92%). Follow-ups for P1.4b: log denied egress (sanitised); apply FORBIDDEN_HEADERS in _GuardTransport; reject decoded non-ASCII paths; SDK client size cap. P1.4c: INV-1 must become a real transport-import scan (httpx/requests/aiohttp/urllib3/httplib2/socket). P4.5: post-DNS private-IP check.
| P1.4b | `src/llm` guard/adapter/pricing/FakeLLM (R4, R5) | backend | P1.4a, P1.2c | merged | | |  | PR #8 round 1 = 78% (2 HIGH: Telegram token in URL passes scrub; spend reservation released before audit write => cap overshoot) -> fixes in progress. Orchestrator owes: pin `openai>=3,<4` in pyproject + NOTICE entries (openai Apache-2.0, httpx2 BSD-3-Clause, httpx BSD already). FOLLOW-UPS: wire PostgresFlagStore/PostgresAuditStore at app wiring (P2.1b) - legacy VLLMClient() stores are process-local so cap/kill switch do not survive restart; /panic must set system_flag llm_kill_switch; base64/spaced canary obfuscation -> P4.4; memory-tagged-message lane check unimplemented; llm_audit has no 'started' state. MERGED 2026-10-25 (PR #8, 07d7d24) after 5 review cycles (78% -> 85.7% -> 85.7% -> 92.9% w/ 1 HIGH -> 14/14); rounds beyond protocol limit of 2 were orchestrator-approved continuation (all findings were R5 spend-cap fail-open paths, fixed as a class: possibly-sent calls are billed or reservation held; cancel-safe). OPEN (disclosed): 500/502/503/429 unbilled+retried under one reservation (record real provider 5xx billing at P5.4 bake-off, PRR evidence before P6.7); held reservation is in-memory only (lost on restart); LOW: hung audit write blocks call (set DB statement timeout in P2.1b). P1.4c: INV-1 transport scan must include httpx2. P2.4: retrieve_memory `memory_manager is None` branch redundant. P1.5: .env.example dead REDIS_URL/VLLM lines. Price 0 rejected (free local model needs tiny non-zero price).
| P1.4c | Delete execute_external + intent-builder tools; INV-1 rewrite; INV-2 delete (M9) | security | P1.4b | merged | | | PROT | PR #11 merged 2026-10-25 (beb5005, 18/19 = 94.7%). Also fixed src/llm/scrub.py:44 quoted-key credential gap (out-of-glob, justified). FOLLOW-UPS -> P1.5 (PROT): INV-1 file covers only TARGET_DESIGN §3 part 2; add part 1 (LLM-SDK scan over src+config; tests/unit/llm/test_llm_import_lint.py scans src only), part 3 (Service.LLM only referenced under src/llm), record parts 4/5 (runtime zero-transport, HF_HUB_OFFLINE) owner; harden scanner: bare import_module/__import__ names, importlib.__import__/builtins.__import__, urllib.request.*/http.client.* attribute chains, asyncio.open_connection. P2.5b: stale scrubber wording in tasks.py/reminders.py/state.py/api routes tasks, ToolIntent fixtures tool:calendar, fake-LLM calendar triggers, test_graph.py:139 assertion weak (assert calendar proposal was made then dropped). W1 gate docs: CLAUDE.md INV-1/INV-2 + src/security line, MEMORY.md AD-12/scrubber ADs superseded, quality/thresholds.yaml:10 comment, .env.example CLOUD_EGRESS_*/GEMINI_MODEL block (H19/H21).
| P1.5 | Module settings pattern, .env.example, scrubbing log filter, INV-7 test | backend | P1.4b | merged | | | PROT | PR #12 merged 2026-10-25 (b9fb56d, 2 rounds: 85.7% -> 92.9%). Log scrub = Logger.makeRecord class-level wrap (src.core.logging.install_log_scrubbing), NOT yet installed by any entrypoint => R12 unmet until P2.1b. P2.1b MUST: call install_log_scrubbing(secrets=[LLM key, canaries]) + test; auth_password/jwt_secret_key -> SecretStr (+ test_shared_settings_errors_and_reprs_hide_values); warm scrub import at install (first log call ~2s); consider logging.raiseExceptions=False; DB statement timeout. FOLLOW-UP (MED-A, fix in P2.1b or earlier): non-str extras whose __str__ differs from __repr__ leak (redact str() and repr()); document that Logger subclasses overriding makeRecord / makeLogRecord / direct LogRecord bypass the scrubber. INV-1 known gaps: getattr(importlib,"import_module"), Service.__members__ enumeration; part 5 HF_HUB_OFFLINE -> P2.3. NOTICE still lists arq (orchestrator).
| P2.1a | Telegram Gateway (long poll, allowlist, persisted offset/updates, rate limit, chunk/escape, no previews) | bot | P1.4a, P1.2c | merged | | | R1–R3 | PR #14 merged 2026-10-25/26 (1eb8808; 71% -> 93%, 2 rounds). ADR-0016 (at-least-once + consumer dedupe on source_key=telegram:<update_id>). OPEN follow-ups: (M1, IMPORTANT, P2.1b/P2.2 acceptance) poison budget (5 deliveries ~15s) can silently lose a legit update during transient consumer failure -> require minimum age (>=10 min since received_at) before poison AND send user a fixed "could not process message at HH:MM, resend" notice; P2.2 must store the event with NO network I/O before ack; (M2) Gateway protocol docstring must state redeliveries are bounded, consumer acks before pulling next item; (L1) poison audit ambiguous in DB (processed_at set + event_id NULL also matches callbacks) -> record poisoned ids in system_flag telegram.poisoned and fix ADR wording; (L2) test counter cleanup on complete + add hot-loop guard/timeouts to poison tests; (L3) begin() re-check processed_at after lock; (L4) 429 retry bypasses RateLimiter.acquire; (L5) answer_callback 200-limit counts code points not UTF-16; sanitize keeps lone surrogates; (L6) gateway integration tests use create_all not migrations (triggers unexercised); P2.1b: install_log_scrubbing([token]) at startup, supervise loop (DB errors escape inbound()), .env.example TELEGRAM_BOT_TOKEN/TELEGRAM_ALLOWED_USER_ID (H19 list). ADR-0016 acknowledged. List residual silent-loss window in PRR reliability section.
| P2.1b | App entrypoint (one process, 3 loops) + delete src/api + FastAPI/JWT deps | bot | P2.1a, P1.3b | todo | | |  |
| P2.2 | Ingest + Event/Transcript contracts; retire SessionBundle/DiarizedTranscript; extractor retyped | bot | P2.1a, P1.4b | todo | | |  |
| P2.3 | Embedder: batch/async Qwen3 local + API fallback in llm; measure on arm64 | memory | P1.3b, P1.4b | review | | | ARM latency needs H6 (or QEMU estimate, flagged) |
| P2.4 | Retrieval rerank + bi-temporal as_of + untrusted filters; LoCoMo re-baseline + provisional-gate ADR | memory | P2.3 | todo | | | PROT |
| P2.5a | Orchestrator plain tool loop + context + exposure (≤12) + interim DenyAllWritesPolicy | orchestrator(backend) | P1.4b, P1.3b | review | | |  |
| P2.5b | Delete Approver, LangGraph, ToolIntent/ToolResult; Tool protocol v2 for tasks/reminders | backend | P2.5a, P1.4c | todo | | | PROT, cf. Red team returns in P4.7 |
| P2.6 | Character card loader + R9 guard (marker + not-placeholder CI check) | personality | P1.5 | todo | | | card text = H1 |
| P3.0 | Lanes core: UntrustedText/DataBlock, Quarantine.extract, taint in TurnContext, INV-6 | security | P2.5b, P2.2 | todo | | | PROT |
| P3.1 | Policy engine (table-driven, ≥95% cov) + wire into dispatch; INV-5 | security | P3.0 | todo | | | PROT |
| P3.2 | Approvals: HMAC token, expiry, single-use, user-only, TOTP, cards | security+bot | P3.1 | todo | | |  |
| P3.3 | Scheduler job runner + reminders/tasks persisted + IST resolution + catch-up | backend | P2.5b, P2.1b | todo | | | fake clock |
| P3.4 | Google OAuth one-time loopback flow, encrypted token store, scope-lint | tools | P1.4a | todo | | | H4 for live |
| P3.5 | Calendar tool (list/create/update, idempotent ids, preview) | tools | P3.4, P3.2 | todo | | | recorded fixtures |
| P3.6 | Gmail tool: search/read (untrusted) + draft create only; send-path denylist; R6 test | tools+security | P3.4, P3.0 | todo | | | ADR-0007 |
| P3.7 | Notion tool: search/read (untrusted)/append | tools | P3.0, P1.4a | todo | | | H5 for live |
| P3.8 | Drive read-only tool | tools | P3.4, P3.0 | todo | | | scope per Q4 |
| P3.9 | Commitment ledger wired to chat (candidate → confirm) | memory | P3.2, P2.4 | todo | | |  |
| P4.1 | Quarantine red-team: email/invite/doc/recorded speech categories | security | P3.6, P3.7 | todo | | |  |
| P4.3 | Exfil: markdown image/URL stripping, previews off, reply URL allowlist | security | P2.1a | todo | | |  |
| P4.4 | Canary strings + outbound blocking + alert | security | P1.4b, P2.1a | todo | | |  |
| P4.5 | Container egress: compose networks (pg internal-only), host nftables outbound 443/53/123 only; doc honest limit | devops | P2.1b | todo | | | ADR-0004 |
| P4.6 | Memory poisoning: untrusted never auto-promoted; review queue | memory+security | P2.4, P3.0 | todo | | |  |
| P4.7 | Red-team corpus ≥100 cases, 12 categories, compromised-FakeLLM, CI | eval-qa | P4.1–P4.6 | todo | | | PROT |
| P4.8 | Security review pass over diff since baseline | security+reviewer | P4.7 | todo | | |  |
| P5.1 | Proactive jobs: morning brief, check-in, nudges, weekly review; budget ≤6/day, quiet hours, snooze | backend | P3.3, P3.9 | todo | | | ADR-0011 |
| P5.2 | Trait model + "forget that about me" veto | personality | P2.4 | todo | | |  |
| P5.3 | Friction/stance live with real model; re-run personality evals | personality+eval-qa | P5.4 | todo | | | PROT, H3 |
| P5.4 | Bake-off harness (evals, tool-calling, injection, transcription incl. ogg, latency/cost) | eval-qa | P4.7 | todo | | | H3 |
| P5.5 | Model ADR + pricing table + spend cap + LoCoMo gate | architect | P5.4 | todo | | |  |
| P6.1–P6.8 | Unchanged from SDLC (Dockerfile arm64 with HF weights baked, deploy/, backups+restore drill, observability, staging soak, UAT, PRR, runbooks) | devops/eval-qa/reviewer | W5 | todo | | | H6–H9 |
| P7.1–P7.5 | Unchanged (docs, CLAUDE/MEMORY consolidation incl. INV table §7, supersede banners, final PR, post-mortem) | docs/architect | W6 | todo | | | H10 |

## Open branches / worktrees / PRs
_none_

## Decisions since last wave
- **Branch naming:** protocol's `pivot/<id>-<slug>` is impossible while branch `pivot` exists (git ref D/F conflict). Task branches are `task/<id>-<slug>`, base `pivot`. PR title format unchanged.
15 ADRs in `docs/decisions/0001-0015` (P0.4): plain Postgres+pgvector (no AGE), Brief INV numbering canonical, `src/llm`+`src/net` sole egress, LangGraph and Approver removed, Postgres SKIP LOCKED jobs, deterministic policy + HMAC approvals. Open deviations needing human OK: ADR-0007 (Gmail compose scope), ADR-0010 (short untrusted fields skip quarantine call), ADR-0004 (app-level egress allowlist).

## Blockers
- H14 approved by user 2026-10-25 (scope: TARGET_DESIGN Q1). PROT tasks unblocked; edits to tests/eval/** limited to that scope.
- P3.4, P3.6 blocked on H16 (Gmail scope, ADR-0007) - do not build the ADR default before the human answers.
- H15 resolved: Docker running.
- ADR-0010 Proposed (H18); blocks P3.0 lanes design.
- PR #1 merged (ffccc5a) after 3 review rounds (73/69/91%).

## Notes
- envcheck (W0 start): REQ tools all OK (git, gh authed as devampatel03, uv 0.8.17, python 3.14.3, docker 27.0.3). All optional env vars missing (Telegram, LLM, Google, Notion, healthcheck) -> tracked H2-H8; FakeLLM/fixtures until provided.
- `.claude/settings.json` in working tree is the kit's MERGE template and has clobbered the original hooks (pre-tool-guard, protect-files, enforce-boundary, graphify). NOT committed; HEAD version is the original. See HUMAN_TODO H12.

## Next 3 actions
1. W2 batch A running: P2.1a (gateway), P2.3 (embedder), P2.5a (loop). Then batch B: P2.2 (ingest), P2.6 (card loader), P2.1b (entrypoint), P2.4, P2.5b.
2. W1 GATE CLOSED 2026-10-25: PR #13 e797686 (reviewer 90% -> fixes -> merged). Legacy Approver (INV-5 violation, HIGH until W2) removed in P2.5b.
3. Fold P1.4a follow-ups into P1.4b/P1.4c (see P1.4a row).
