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
| P1.0 | Delete dead weight (M0) | backend | – | todo | | | pure deletion |
| P1.1 | CI on pivot: ruff/mypy/pytest + pgvector svc + red-team job + secret scan | devops | – | todo | | |  |
| P1.2a | Postgres image AGE→pgvector | devops | – | todo | | | recreate dev volume |
| P1.2b | Alembic baseline: personality tables revision + upgrade==ORM test | backend | P1.2a | todo | | |  |
| P1.2c | Pivot schema: event/memory_chunk/job/llm_audit/system_flag/pending_action/inbound_update; provenance cols; append-only triggers; db/crypto.py | backend+memory | P1.2b | todo | | | PROT |
| P1.3a | Redis out (M5) | memory | P1.2c | todo | | | PROT, cf |
| P1.3b | pgvector MemoryStore episodic + Qdrant out (M6) | memory | P1.3a | todo | | | PROT, cf |
| P1.4a | `src/net` egress client + R13 test | security | P1.1 | todo | | | parallel with P1.2*/P1.3* |
| P1.4b | `src/llm` guard/adapter/pricing/FakeLLM (R4, R5) | backend | P1.4a, P1.2c | todo | | |  |
| P1.4c | Delete execute_external + intent-builder tools; INV-1 rewrite; INV-2 delete (M9) | security | P1.4b | todo | | | PROT |
| P1.5 | Module settings pattern, .env.example, scrubbing log filter, INV-7 test | backend | P1.4b | todo | | | PROT |
| P2.1a | Telegram Gateway (long poll, allowlist, persisted offset/updates, rate limit, chunk/escape, no previews) | bot | P1.4a, P1.2c | todo | | | R1–R3 |
| P2.1b | App entrypoint (one process, 3 loops) + delete src/api + FastAPI/JWT deps | bot | P2.1a, P1.3b | todo | | |  |
| P2.2 | Ingest + Event/Transcript contracts; retire SessionBundle/DiarizedTranscript; extractor retyped | bot | P2.1a, P1.4b | todo | | |  |
| P2.3 | Embedder: batch/async Qwen3 local + API fallback in llm; measure on arm64 | memory | P1.3b, P1.4b | todo | | | ARM latency needs H6 (or QEMU estimate, flagged) |
| P2.4 | Retrieval rerank + bi-temporal as_of + untrusted filters; LoCoMo re-baseline + provisional-gate ADR | memory | P2.3 | todo | | | PROT |
| P2.5a | Orchestrator plain tool loop + context + exposure (≤12) + interim DenyAllWritesPolicy | orchestrator(backend) | P1.4b, P1.3b | todo | | |  |
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
- PROT tasks wait on H14 (batch approval to edit tests/eval/**).
- P3.4, P3.6 blocked on H16 (Gmail scope, ADR-0007) - do not build the ADR default before the human answers.
- H15: Docker daemon down; blocks P1.2b onward integration verification.
- ADR-0010 Proposed (H18); blocks P3.0 lanes design.
- PR #1 merged (ffccc5a) after 3 review rounds (73/69/91%).

## Notes
- envcheck (W0 start): REQ tools all OK (git, gh authed as devampatel03, uv 0.8.17, python 3.14.3, docker 27.0.3). All optional env vars missing (Telegram, LLM, Google, Notion, healthcheck) -> tracked H2-H8; FakeLLM/fixtures until provided.
- `.claude/settings.json` in working tree is the kit's MERGE template and has clobbered the original hooks (pre-tool-guard, protect-files, enforce-boundary, graphify). NOT committed; HEAD version is the original. See HUMAN_TODO H12.

## Next 3 actions
1. Run W1 batch {P1.0, P1.1, P1.2a}; then {P1.2b, P1.4a}.
2. Hold PROT tasks (P1.2c onward) until H14; P3.4/P3.6 until H16; P3.0 until H18.
3. Docker Desktop (H15) must be up before P1.2b.
