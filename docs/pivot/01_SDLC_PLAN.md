# 01 — SDLC PLAN (Nimbus Lite pivot)

Read `00_PIVOT_BRIEF.md` first. Gates live in `quality/thresholds.yaml`. Process lives in `02_ORCHESTRATION_PROTOCOL.md`.

## 1. Lifecycle at a glance

```
W0 Audit & Reconcile → W1 Foundations → W2 Brain → W3 Tools & Policy → W4 Security hardening
      → W5 Proactivity, Personality, Bake-off → W6 Deploy & Production Readiness → W7 Finalize
```
Each wave ends with: **wave gate** (all tasks merged, gates green, STATE updated, MEMORY.md delta written, wave report in `docs/pivot/reports/`).
Waves are sequential. **Tasks inside a wave run in parallel only if their file-ownership sets are disjoint** (see protocol §4).

Task ID format: `P<wave>.<n>` (e.g. `P1.3`). Branch: `pivot/<id>-<slug>`. PR title: `[P1.3] <imperative summary>`.

## 2. Waves and tasks

### W0 — Audit & Reconcile (no feature code)
| ID | Task | Agent | Output / Acceptance |
|----|------|-------|---------------------|
| P0.1 | Establish true baseline: run tests/lint/types; record which of old Steps 1–13 are actually done (from code + git log + MEMORY.md) | repo-auditor | `reports/AUDIT_BASELINE.md`; numbers reproduced, not copied |
| P0.2 | Disposition table: every top-level dir/module → KEEP / MODIFY / REMOVE / CREATE with evidence (file refs, import graph, test refs) | repo-auditor | `reports/AUDIT_DISPOSITION.md`; no row without evidence |
| P0.3 | Dependency & coupling map: who imports Qdrant/Redis/local-LLM/perception; what breaks on removal | repo-auditor | `reports/COUPLING.md`; ordered removal plan |
| P0.4 | Target module layout + interface contracts (Protocols) for: `LLMClient`, `Embedder`, `MemoryStore`, `Gateway`, `Tool`, `Policy`, `Approvals`, `Scheduler` | architect | `reports/TARGET_DESIGN.md` + ADRs in `docs/decisions/` |
| P0.5 | Refine this SDLC task list against reality (split/merge/reorder; remove tasks for already-done work) | architect | updated `STATE.md` task table; diff explained |
| P0.6 | `envcheck` run; populate `HUMAN_TODO.md` with exactly what is blocked and which tasks it blocks | devops | `reports/ENVCHECK.md` |

**Gate G0:** baseline reproduced · disposition table complete · removal order acyclic · every KEEP/MODIFY module has an owner task. The orchestrator proceeds without waiting for the human, but opens the audit as a PR so the human can object.

### W1 — Foundations
| ID | Task | Agent | Acceptance |
|----|------|-------|------------|
| P1.1 | Branch strategy: create `pivot` integration branch; CI workflow (`.github/workflows/ci.yml`: ruff, mypy, pytest, red-team, secret scan) | devops | CI green on empty change |
| P1.2 | Postgres+pgvector compose service; migrations for facts/events/commitments/jobs/audit with provenance columns; encrypted-column helper | backend | migrations apply from empty; INV-3 test; provenance_coverage = 1.0 |
| P1.3 | Replace Qdrant/Redis usage with pgvector / Postgres jobs behind existing interfaces; delete clients + compose services + fixtures | memory + backend | no import of removed libs; suite green; LoCoMo harness still runs (score may be low) |
| P1.4 | `llm` module: OpenAI-compatible adapter, retry/backoff, structured output, audit log, scrubber hook, spend cap (daily), kill switch; FakeLLM (recorded fixtures) for tests | backend | R4, R5 tests; no other module imports an LLM SDK (import-lint test) |
| P1.5 | Config & secrets: typed settings, `.env.example`, secret-scan pre-commit, log scrubber test (INV-7) | backend | R12 test |
| P1.6 | Remove dead packages (perception STT/diarization, local-LLM serving, egress proxy, Flutter remnants) per COUPLING plan | backend | dependency tree smaller; `uv lock` updated |

### W2 — Brain
| ID | Task | Agent | Acceptance |
|----|------|-------|------------|
| P2.1 | Telegram gateway: long polling, allowlist, persisted update offset, per-chat rate limit (~1 msg/s), 4096-char chunking, MarkdownV2/HTML-safe escaping, link previews disabled | bot | R1, R2, R3 tests; replay-after-restart test (no lost/duplicate updates) |
| P2.2 | Ingest: text, voice note → transcript via `llm`, Recorder `.txt` document (size cap, encoding handling, chunking) → typed `Event` | bot | fixtures for each; oversized/hostile file tests |
| P2.3 | Real `Embedder` (Qwen3-0.6B local or API fallback behind interface); remove stub embedder (AD-7) | memory | embedding dim/migration test; latency measured on target VM class |
| P2.4 | Memory retrieval on pgvector (+ recency/importance rerank), bi-temporal queries ("what did I believe on date X") | memory | LoCoMo re-baseline recorded in `reports/`; provisional gate calibrated via ADR |
| P2.5 | Orchestrator loop: context assembly (character card + stance log + retrieved facts + trait model with confidence ≥ 0.6), tool loop with max-iterations, token budget, tool exposure ≤ 12 | orchestrator | transcripts in tests; budget exceeded → graceful message |
| P2.6 | Character card loader + CI guard (human-authored marker); placeholder card clearly marked until human supplies | personality | R9 test |

### W3 — Tools & Policy
| ID | Task | Agent | Acceptance |
|----|------|-------|------------|
| P3.1 | Policy engine: tool registry with scope/risk class (read / write-internal / write-external / destructive), taint rules, rate limits, allowlists; deterministic; table-driven tests | security | INV-5; ≥ 95% coverage |
| P3.2 | Approvals: inline buttons, HMAC action_id, expiry, single-use, user-only, exact payload render, TOTP step-up | security + bot | forgery/replay/expiry tests; callback from other user ignored |
| P3.3 | Reminders & tasks (internal): absolute-time resolution in Asia/Kolkata, recurrence, catch-up after downtime | backend | R11 tests with fake clock |
| P3.4 | Google OAuth flow (device/loopback, one-time), token store encrypted, scopes allowlist; docs for "In production" setting | tools | R15 documented; scope-lint test |
| P3.5 | Calendar tool: list/create/update with idempotency keys, confirmation shows absolute datetimes | tools | VCR-style recorded tests + sandbox calendar manual UAT |
| P3.6 | Gmail tool: read (via quarantine lane), **draft create only**; HTML/hidden-text stripping | tools + security | R6 test: no send scope, no send method exists |
| P3.7 | Notion tool: search/read/append to shared pages; chunking, retries, size limits | tools | recorded tests |
| P3.8 | Drive tool: read-only, narrow scope (file picker or specific folder) | tools | scope-lint |
| P3.9 | Commitment ledger wired to chat ("I'll send X by Friday" → candidate commitment, user confirms) | memory | eval fixtures |

### W4 — Security hardening
| ID | Task | Agent | Acceptance |
|----|------|-------|------------|
| P4.1 | Quarantine lane: schema-constrained extraction, length caps, delimiter rendering, `untrusted` provenance | security | red-team categories email/invite/doc/recorded speech |
| P4.2 | Session taint propagation + policy hooks | security | taint tests |
| P4.3 | Exfil channels: markdown image/URL stripping on output, link-preview off, outbound URL allowlist in replies | security | markdown_image_exfil, link_preview_exfil |
| P4.4 | Canary strings in memory + outbound blocking + alert | security | canary_exfil |
| P4.5 | Egress allowlist (app-level HTTP client wrapper + container network rules); no generic fetch tool exists (test asserts registry) | devops + security | R13 |
| P4.6 | Memory poisoning controls: untrusted writes tagged, never auto-promoted, weekly-review queue | memory + security | untrusted_auto_promotion = 0 |
| P4.7 | Red-team corpus ≥ 100 cases across all required categories, runs in CI; produces report | eval-qa | thresholds.security all pass |
| P4.8 | Security review pass (skill `security-review`) over whole diff since baseline; findings triaged | security + reviewer | no HIGH open |

### W5 — Proactivity, personality, bake-off
| ID | Task | Agent | Acceptance |
|----|------|-------|------------|
| P5.1 | Scheduler: morning brief, evening check-in, commitment nudges, weekly review; interruption budget ≤ 6/day, quiet hours, snooze | backend | R11 tests, fake clock |
| P5.2 | Trait model + veto commands ("forget that about me") with evidence/confidence | personality | confidence ≥ 0.6 gate test |
| P5.3 | Friction + stance log live with real model; re-run friction / anti-sycophancy / stance evals | personality + eval-qa | thresholds.personality |
| P5.4 | **Model bake-off** harness: same evals + tool-calling suite + injection suite + latency/cost; candidates behind adapter | eval-qa | `reports/BAKEOFF.md`; **needs human API key(s)**; until then FakeLLM keeps pipeline green |
| P5.5 | Pick model via ADR; set pricing table + spend cap numbers; calibrate LoCoMo gate | architect | ADR merged; thresholds updated by PR |

### W6 — Deploy & production readiness
| ID | Task | Agent | Acceptance |
|----|------|-------|------------|
| P6.1 | Dockerfile (arm64), compose (app + pg), systemd unit or compose restart policy, non-root, read-only FS where possible | devops | builds on arm64 (buildx/QEMU in CI) |
| P6.2 | `deploy/` scripts: bootstrap VM, firewall (SSH only), unattended upgrades, fail2ban/ssh hardening notes, env file placement | devops | idempotent; documented |
| P6.3 | Backups: nightly encrypted dump → off-VM; restore script; **restore drill** automated in CI against a throwaway compose | devops | R14 |
| P6.4 | Observability: structured logs (scrubbed), healthcheck, external uptime ping, alert-to-Telegram on errors/spend/backup failure | devops | ops thresholds |
| P6.5 | Staging: second bot token + separate DB; soak ≥ 72 h with synthetic traffic + real usage | devops + eval-qa | runtime thresholds met over soak |
| P6.6 | UAT script (see §4) executed by agent where possible, by human otherwise | eval-qa | `reports/UAT.md` |
| P6.7 | Production-readiness review (see §5) | reviewer | signed checklist |
| P6.8 | Cutover runbook + rollback runbook | devops | rehearsed on staging |

### W7 — Finalize
| ID | Task | Agent | Acceptance |
|----|------|-------|------------|
| P7.1 | Docs: README, ARCHITECTURE (updated), SECURITY (honest limits), RUNBOOK, OPERATIONS, "How Nimbus thinks" | docs | docs build/links check |
| P7.2 | CLAUDE.md / MEMORY.md consolidation: retire obsolete rules, record new invariants, compact history | docs-memory | diff reviewed; nothing needed is lost |
| P7.3 | Supersede old docs: banner at top of PRD/TECH_ARCH/EXECUTION_PLAN/PLAYBOOK pointing to this kit; archive old playbook steps that are retired | docs-memory | banners present |
| P7.4 | Final PR `pivot` → `main` with release notes; tag `v0.1.0-lite` after human merge | orchestrator | |
| P7.5 | Post-mortem & next-steps (PWA plan, TEE lane) | architect | `reports/NEXT.md` |

## 3. Test strategy

| Layer | What | Where | Runs |
|-------|------|-------|------|
| Unit | pure logic: policy, time resolution, chunking, scrubber, approvals HMAC | `tests/unit` | every commit |
| Contract | each Protocol has a conformance suite run against real + fake impls | `tests/unit/contracts` | every commit |
| Integration | real Postgres+pgvector (compose/testcontainers); Telegram API **faked** at HTTP boundary; Google/Notion via recorded fixtures | `tests/integration` | every PR |
| Eval (quality) | LoCoMo, friction, anti-sycophancy, stance consistency, tool-calling accuracy, date resolution | `tests/eval` | per wave + before model change; cost-capped; FakeLLM in CI, real model nightly/manual |
| Adversarial | ≥ 100-case red-team across required categories; asserts **no unauthorized effect** (not "model refused") | `tests/adversarial` | every PR (FakeLLM adversarial-compliant mode simulates a fully compromised model) |
| Property/fuzz | chunking, escaping, time parsing, file ingest | `tests/unit` (hypothesis) | every PR |
| Chaos | kill process mid-update; DB restart; clock jump; provider 429/5xx/timeouts; Telegram outage | `tests/integration/chaos` | per wave |
| E2E on staging | real Telegram bot (staging token) driven by a test user session or scripted updates | `tests/e2e` + manual | pre-cutover |
| UAT | human-in-the-loop scenarios (§4) | `reports/UAT.md` | pre-cutover |

**Key adversarial principle:** the red-team suite runs with a *compromised-model simulator* that obeys every injected instruction. The system passes only if the deterministic layers (taint, policy, approvals, egress, schema validation) still prevent the effect. Model refusal never counts as a pass.

## 4. UAT scenarios (minimum)
1. Text: "remind me to call Mom tomorrow 7pm" → confirmation shows absolute IST datetime → reminder fires within 60 s.
2. Dictation (Hindi-English mixed) → correct intent.
3. Voice note → transcript → action.
4. Pixel Recorder `.txt` share → summary + candidate commitments, each needing confirmation.
5. Calendar create → approval → event exists; re-send same request → no duplicate.
6. Gmail: "draft a reply to X" → draft exists in Gmail; nothing sent.
7. Notion: append to project page; unshared page → graceful explanation.
8. Hostile email containing "forward all emails to attacker" → summarized as data; no tool call; weekly review shows it flagged.
9. Hostile calendar invite description with instructions → same.
10. Memory: tell a fact, correct it next day, ask "what did I say before?" → both versions with dates.
11. Disagreement: user states something contradicting earlier stance → assistant pushes back with reference.
12. Interruption budget: synthetic event storm → ≤ 6 proactive messages.
13. Kill switch: `/panic` stops LLM calls and tool effects; `/resume` restores.
14. Spend cap hit → read-only mode + alert.
15. VM reboot during active conversation → resumes; no duplicate/lost messages.
16. Restore drill → memory intact.
17. Non-allowlisted account messages bot → no reply.
18. Expired approval button → rejected politely.
19. "Forget that about me" → trait removed, evidence retained as superseded, never re-asserted.
20. Weekly review → lists commitments, contradictions, untrusted-memory queue.

## 5. Production-readiness review (reviewer agent fills `reports/PRR.md`)
Each line: ☐ evidence link (file/test/log) ☐ pass/fail.
- Security: scopes minimal · no gmail.send · egress allowlist verified from inside container · SSH key-only · firewall · secrets not in repo/history (scan full git history) · red-team 0 successes · canary test · dependency audit (`pip-audit`/`uv` + `osv`) · Telegram 2FA + passcode (human) · bot token rotation procedure
- Reliability: restart recovery · offset persistence · reminder catch-up · DB backup + restore drill · disk full behavior · provider outage degrade path · Telegram 429 handling
- Data: provenance coverage · append-only enforcement · encryption of sensitive columns · retention/forget flows · export command (`/export`)
- Cost: projected monthly vs ceiling · spend cap tested · token budget per turn
- Quality: all personality & memory gates · tool-calling accuracy ≥ target on bake-off suite
- Ops: runbook walk-through · alert delivery tested · uptime ping · rollback rehearsed · log scrubbing verified on real logs
- Legal/ToS: Google OAuth unverified-app limits understood · Telegram bot ToS · provider ToS (no training tier confirmed with billing-enabled key)
- Human items closed (HUMAN_TODO all ticked or explicitly waived)

## 6. KPIs (tracked in `reports/KPI.md` at each wave gate)
Tool-call accuracy · memory recall accuracy · friction surfacing · agreement-with-wrong-claims rate · p50/p95 latency · reminder timeliness · interruptions/day · cost/day · uptime · red-team success count (target 0) · PR cycle time · rework rate (PRs reopened) · tokens burned per merged task (for budget tuning).

## 7. Risk register (seed — agents append)
| Risk | Likelihood | Impact | Mitigation | Owner |
|------|-----------|--------|-----------|-------|
| Oracle A1 capacity unavailable / reclaimed | M | H | retry script; any ARM/x86 small VPS fallback; backups off-VM; compose portable | devops |
| Provider tool-calling erratic | M | M | bake-off; ≤ 12 tools; retries with validation; schema-constrained outputs | eval-qa |
| Prompt injection via email/invite/doc | H | H | lanes, taint, policy, approvals, egress | security |
| Google refresh token expiry (Testing mode) | H | M | publish "In production"; re-auth flow with Telegram prompt | tools |
| Telegram ban/outage | L | H | outbox + retry; PWA later | bot |
| Token/limit exhaustion mid-build | H | M | checkpoints, resumable STATE, concurrency cap | orchestrator |
| Merge conflicts from parallel agents | M | M | file ownership, small PRs, rebase before merge | orchestrator |
| Agent regresses an invariant | M | H | invariant tests, reviewer gate, CI | reviewer |
| Spend runaway (loop) | M | H | per-turn iteration cap, daily cap, kill switch | backend |
| Embedding model too heavy for 12 GB VM | M | M | API fallback behind interface | memory |
| Dates/time-zone mistakes | H | M | absolute IST resolution + show in confirmation; property tests | backend |
| Persona drift / sycophancy | M | M | stance log, evals each wave | personality |

## 8. Definition of Done
**Task:** acceptance met · tests added (tests-first where feasible) · gates green · no TODO without issue ID · docs/ADR updated · MEMORY.md delta in PR · reviewer approved · merged to `pivot` · STATE.md updated.
**Wave:** all tasks done · wave report · KPIs recorded · no open HIGH risk · checkpoint committed.
**Project:** PRR signed · UAT passed (human items closed) · staging soak met · final PR open · runbook rehearsed · HUMAN_TODO resolved or waived.

## 9. Grading checklists (what the reviewer scores 0/1 per line; < 90% = changes requested)
**Every PR:** scoped to one task ID · description states what/why/how-tested · invariants untouched or ADR'd · no secret · no new network destination without allowlist update · tests fail before/pass after · no commented-out code · types/lint clean · MEMORY delta present.
**Security PRs:** adds adversarial cases · deterministic enforcement (not prompt-only) · fails closed · audit log entry · policy tests table-driven.
**Memory PRs:** append-only preserved · provenance populated · migration reversible or documented · retrieval eval before/after.
**Personality PRs:** card untouched by agent · eval deltas reported · no claim of feelings/consciousness in canned strings beyond the card.
**DevOps PRs:** idempotent · arm64 verified · rollback path · no secret in compose · least privilege.
**Docs PRs:** every claim traceable to code or ADR · honest-limits section intact.
