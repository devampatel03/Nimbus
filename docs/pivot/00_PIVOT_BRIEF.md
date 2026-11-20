# 00 — PIVOT BRIEF (normative)

> Status: **AUTHORITATIVE.** Where this file conflicts with PRD v2, TECHNICAL_ARCHITECTURE v2, EXECUTION_PLAN, or PLAYBOOK_*.md, **this file wins.** Where it conflicts with the repo's `CLAUDE.md` *rules about process* (TDD, memory discipline, one-task-at-a-time hygiene), CLAUDE.md wins. Where it conflicts with an invariant in the repo that this file does not explicitly retire, stop and log an ADR.

## 1. The decision

Nimbus was designed as laptop-hosted, local-model, hardware-adjacent, voice-stack-heavy. That design created friction (laptop must be on, local LLM quality/latency, Flutter app, diarization stack) for a single-user personal assistant that must be **24/7, cheap, human-feeling, and "private to a reasonable level."**

We are pivoting to **Nimbus Lite**:

- **Channel:** Telegram bot (long polling — no public URL, no inbound ports). PWA is a *later* private channel (out of scope now, but do not design it out).
- **Host:** one Oracle Cloud Always Free Ampere A1 VM (assume **2 OCPU / 12 GB** until verified; capacity/reclamation risk is real → backups mandatory).
- **Process:** one Python service (bot + worker + scheduler) + Postgres with `pgvector`.
- **Model:** one OpenAI-compatible adapter pointing at a cheap **paid, no-training-on-data** closed model, chosen by a one-day bake-off on our own evals. No free tier / AI Studio for real data. No hosted DeepSeek API (data residency).
- **Tools:** Google Calendar, Notion, Gmail (**drafts only — no send scope**), Drive (least privilege), reminders/tasks (internal).
- **Embeddings:** local `Qwen3-Embedding-0.6B` on the VM if resources allow, else API embeddings behind the same interface.
- **Input paths:** Gboard dictation, Telegram voice notes (transcribed via the model adapter), Pixel Recorder → Share → Telegram (`.txt` transcript).

**Privacy stance (user-accepted):** Telegram, Oracle, and the model provider can each see plaintext. We mitigate *leak and injection* risk by architecture, not by trusting filters. A later attested-TEE / PWA lane is the upgrade path. Never claim more privacy than this in code comments, docs, or bot replies.

## 2. Architecture

```
 Phone (Telegram app, Gboard dictation, voice notes, Pixel Recorder share)
        │  HTTPS (long polling, outbound only)
        ▼
┌──────────────────────── Oracle A1 VM (single docker compose project) ────────────────────────┐
│  nimbus-app (one Python process)                                                              │
│   ├─ telegram_gateway   allowlist(user_id) · offset store · rate limit · no link previews       │
│   ├─ ingest             text / voice→transcript / recorder .txt → typed Event                   │
│   ├─ lanes                                                                                      │
│   │    ├─ TRUSTED   user DMs → orchestrator (memory + tools)                                    │
│   │    └─ UNTRUSTED email/invite/doc/web/third-party speech → QUARANTINE LLM (no tools,         │
│   │                  no memory) → strict typed JSON → back to trusted lane as DATA only         │
│   ├─ orchestrator       context assembly · character card · tool loop (≤ N tools exposed/turn)   │
│   ├─ policy             deterministic: scopes · allowlists · rate limits · taint · approvals     │
│   ├─ approvals          inline buttons · exact payload shown · signed action_id · user-only      │
│   ├─ tools              calendar · notion · gmail_draft · drive_read · reminders · tasks        │
│   ├─ memory             bi-temporal facts · provenance · commitments · trait model · stance log  │
│   ├─ scheduler          reminders · check-ins · weekly review · interruption budget (≤6/day)    │
│   ├─ llm (ONE module)   adapter · audit log · scrubber · spend cap · kill switch                 │
│   └─ egress allowlist   Telegram · model API · Google · Notion  (nothing else)                   │
│  postgres (+pgvector)   plain tables · encrypted sensitive columns · provenance on every row     │
│  backups                nightly encrypted pg_dump → off-VM (provider-independent)               │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Trust lanes (the core security idea)
A session that has read **untrusted content**, holds **access to private data**, and can **act/communicate externally** is the "lethal trifecta." Nimbus never lets one context have all three without a human approval gate:

1. **Trusted lane** — user-authored messages only. Has memory + tools. Never sees raw untrusted content.
2. **Quarantine lane** — any externally-authored text. One LLM call, **no tools, no memory**, output must validate against a strict Pydantic schema. Free-text fields are length-capped, stored as DATA with `untrusted=true`, and rendered to the trusted lane inside clearly delimited data blocks.
3. **Session taint flag** — once untrusted-derived data enters a trusted turn, external-effect tools require approval (not just destructive ones).
4. **Policy layer is code, not a prompt.** The approver LLM from the old design is **replaced** by deterministic policy + user approval buttons.

### Approvals
Inline buttons show the **exact payload** (resolved absolute `Asia/Kolkata` datetimes, recipients, body). `action_id` is an HMAC over (payload, nonce, expiry); only callbacks from the allowlisted user ID are accepted; single-use; expires (default 10 min). TOTP step-up for external-recipient or destructive actions.

## 3. Hard requirements (R-numbers are referenced by tests and PRs)

| ID | Requirement |
|----|-------------|
| R1 | Works 24/7 with no laptop dependency; survives VM reboot (systemd/compose restart, offset persisted, reminders catch up) |
| R2 | Only the allowlisted Telegram user ID is ever served; others get silence (logged) |
| R3 | No inbound ports except SSH (key-only, ideally restricted); bot uses long polling |
| R4 | Every LLM call goes through the single `llm` module (audit log, scrubber, spend cap, kill switch) |
| R5 | Daily spend cap enforced in code; exceeding it degrades to "read-only/notify" and alerts the user |
| R6 | Gmail: drafts only. No `gmail.send` scope requested. Ever. |
| R7 | Memory is append-only/bi-temporal; every fact has provenance (source event, lane, confidence) |
| R8 | Untrusted-derived memory writes are tagged and never auto-promoted; surfaced in weekly review |
| R9 | Assistant personality comes from a **hand-authored** character card (CI fails if generated or edited by an agent without a human-approved marker) |
| R10 | Assistant states disagreement when warranted (friction), stays consistent with its stance log, resists sycophancy — measured by evals |
| R11 | ≤ 6 proactive interruptions/day, with quiet hours; reminders delivered within 60 s of due |
| R12 | Secrets only via env/secret store; never in repo, logs, or LLM context; log scrubber enforced |
| R13 | Outbound egress restricted to an allowlist; no generic web-fetch tool |
| R14 | Restore drill passes: backup → fresh VM/compose → bot answers with memory intact |
| R15 | Google OAuth app set to **In production** (unverified, personal use) so refresh tokens don't die at 7 days |
| R16 | Tool exposure per turn kept small (≤ 12 by default; dynamic selection above that) |

## 4. Invariants (supersedes older list; mapping to old numbering)

- **INV-1 (reframed):** exactly one module may call an LLM; it keeps a local audit log, scrubs PII/secrets, enforces spend cap, and has a kill switch. *(old INV-1 "nothing leaves the machine" is retired.)*
- **INV-2 (retired):** local-only inference. Do not enforce; remove tests that assert it, replacing with R4/R13 tests.
- **INV-3 (kept):** memory append-only with supersession; no destructive update/delete of facts.
- **INV-4 (moot):** mode-A/B voice routing. Remove.
- **INV-5 (new):** policy decisions are deterministic code paths with unit tests; no LLM output may grant a permission.
- **INV-6 (new):** untrusted text never enters the trusted lane except as schema-validated, delimited DATA.
- **INV-7 (new):** no secret or token value ever appears in logs, prompts, memory, or error messages.

## 5. Repo disposition — HYPOTHESES for the audit to confirm or overturn

The audit (W0) must verify each row **from the code**, not from this table. Evidence > this table.

| Area | Hypothesis | Likely action |
|------|-----------|---------------|
| Qdrant / Redis clients, docker services, `tests/conftest.py` fixtures | replaced by Postgres+pgvector (+ in-process queues / Postgres for jobs) | REMOVE (after migrating data paths + tests) |
| Local LLM serving (Ollama/vLLM, old PLAYBOOK Step 12) | not needed | REMOVE / do not build |
| STT/TTS voice stack (old Step 18), diarization (Parakeet/pyannote/CAM++, old Step 19) | replaced by Gboard / voice-note-via-model / Pixel Recorder | REMOVE / do not build |
| Egress proxy (old Step 20) | replaced by simple allowlist at app + container-network level | REMOVE / simplify |
| Flutter app (old Steps 21–22), wearable | out of scope | REMOVE / do not build |
| `src/memory` | bi-temporal store, provenance | KEEP; MODIFY storage backend to pgvector; keep contracts |
| `src/personality` (character card, stance log, friction, trait model) | core differentiator | KEEP; wire to real model; keep evals |
| `src/security` (scrubber, PII detector, audit, kill switch, approver heuristic) | scrubber/audit/kill-switch reusable; approver heuristic obsolete | KEEP scrubber/audit/kill; REPLACE approver with policy + approvals |
| `src/orchestrator` | tool loop likely local-model-shaped | MODIFY for OpenAI-compatible adapter, lanes, taint |
| `src/api` | HTTP API likely not needed for Telegram-first | MODIFY → minimal health endpoint (bound to localhost) or REMOVE |
| `src/perception` | local STT/diarization | REMOVE; replace with ingest (voice-note/transcript) |
| `src/tools`, `src/workers` | partial | MODIFY / CREATE Google, Notion, scheduler |
| `src/contracts`, `src/db` | pydantic contracts, migrations | KEEP; MODIFY migrations for pgvector, drop Qdrant/Redis coupling |
| `tests/{unit,integration,eval,adversarial,fixtures}` | 183 pass / 1 skip at last report | KEEP structure; update; add red-team (CI) |
| `.claude/` harness (hooks, agents bug-hunter / intent-checker, commands) | reusable | KEEP; ADD pivot kit; do not clobber `settings.json` — merge |

Pre-existing known gaps to resolve, not rediscover: stub embedder (AD-7) → real embedder; LoCoMo harness at 6.4% (stub) → re-baseline with real embedder + model; anti_sycophancy / stance_consistency harnesses are placeholder baselines → real model runs; injection red-team 24/30 → replaced by architectural gate (0 successful unauthorized actions).

## 6. Deferred (design must not preclude)

PWA private channel + Gemini Live voice · Discord voice experiment · attested-TEE model lane (NEAR AI / Phala / Tinfoil; GLM-Flash-class candidate) · Matrix/Conduit E2EE · multi-user.

## 7. Open decisions with defaults (log an ADR when resolved)

| Decision | Default until resolved | Resolver |
|----------|-----------------------|----------|
| Model provider | adapter + recorded-fixture fake for tests; bake-off candidates: Gemini Flash-Lite-class, Haiku-class, GPT nano/mini-class | W5 bake-off (needs human key) |
| Embeddings | Qwen3-Embedding-0.6B local if VM RAM headroom ≥ 3 GB, else API | W2 |
| Job queue | Postgres `SKIP LOCKED` jobs (no Redis) | W1 |
| Secrets at rest | env file with 0600 + column-level encryption key from env; revisit age/sops | W1 |
| Backup target | encrypted dump → user's Drive folder or a second free object store | W6 |
| Final merge to `main` | human merges one final PR | STATE.md setting |

## 8. Honest limits (never overstate in docs or bot replies)

- Telegram bot chats are **not** end-to-end encrypted.
- Provider "no training" ≠ zero retention (abuse-monitoring retention can be up to ~30 days depending on provider/tier — verify at bake-off).
- Oracle can reclaim or cap Always Free resources; treat the VM as disposable.
- Prompt-injection cannot be eliminated; the design bounds the **blast radius**.
