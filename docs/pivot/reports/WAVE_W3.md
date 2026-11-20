# WAVE W3 report — Nimbus Lite tools, lanes, policy and approvals, gate

Date: 2026-10-29. Branch `task/W3-gate-docs`, worktree `C:/DEVAM/MY_PROJECTS/Nimbus-W3-gate`, from `pivot` @ `a1614e8`. Docs-only PR into `pivot`. No code, no `tests/eval/**`, no character card.
Numbers come only from commands run on that checkout (Windows 11, `uv sync --extra dev`, compose `postgres` up, `POSTGRES_*` as in CI). KPI section: `docs/pivot/reports/KPI.md`. No model was loaded, no provider, Google, Notion or Telegram was called live.

## 1. What shipped in W3 (PRs into `pivot`)

Merge times are UTC from GitHub `mergedAt`; SHAs from `git log pivot`.

| PR | Task | Merged (UTC), SHA | Result (STATE) |
|---|---|---|---|
| [#28](https://github.com/devampatel03/Nimbus/pull/28) | P3.4 Google OAuth loopback, encrypted token store, scope-lint (ADR-0019) | 2026-10-27 13:10, `5f6a7ec` | 78.6% → 100% |
| [#29](https://github.com/devampatel03/Nimbus/pull/29) | P3.0 lanes core: UntrustedText/DataBlock, `Quarantine.extract`, taint, INV-6 (interim ADR-0010 default) | 2026-10-27 13:39, `241f64a` | 93% → 100% |
| [#30](https://github.com/devampatel03/Nimbus/pull/30) | P3.3 scheduler: Postgres job runner, persisted reminders/tasks, IST resolution, `memory_extract` write path | 2026-10-28 07:00, `0b1a565` | 84.6% → 100% (3 rounds) |
| [#31](https://github.com/devampatel03/Nimbus/pull/31) | P3.1 deterministic policy engine wired into dispatch (INV-5) | 2026-10-28 15:26, `c9986f0` | merged |
| [#32](https://github.com/devampatel03/Nimbus/pull/32) | P3.3 follow-up: drop apscheduler, pin tzdata | 2026-10-28 07:12, `78bc611` | merged |
| [#33](https://github.com/devampatel03/Nimbus/pull/33) | P3.1 follow-up: `run_app` wiring test, comment fixes | 2026-10-28 15:49, `1019f76` | merged |
| [#34](https://github.com/devampatel03/Nimbus/pull/34) | P3.0 follow-up: flaky malformed-nonce test | 2026-10-28 15:49, `e02c588` | merged |
| [#35](https://github.com/devampatel03/Nimbus/pull/35) | P3.2 approvals: HMAC token, atomic single-use consume, TOTP step-up, cards, `policy_audit` (ADR-0020) | 2026-10-28 18:20, `99fbfa4` | 94.1% → 100% |
| [#36](https://github.com/devampatel03/Nimbus/pull/36) | P3.8 Drive read-only tool | 2026-10-28 22:20, `18734a8` | 92.9% → 100% |
| [#37](https://github.com/devampatel03/Nimbus/pull/37) | P3.5 Google Calendar tools (opaque handles, idempotent ids, IST card) | 2026-10-28 22:49, `f020c6e` | 78.6% → 92.9% (3 passes) |
| [#38](https://github.com/devampatel03/Nimbus/pull/38) | P3.8 follow-up: offer only tools that can run (R16 cap) | 2026-10-28 21:33, `7f36cf6` | merged |
| [#39](https://github.com/devampatel03/Nimbus/pull/39) | P3.5 pre-wiring hardening: Calendar and Drive | 2026-10-29 02:05, `071b0ea` | merged |
| [#40](https://github.com/devampatel03/Nimbus/pull/40) | P3.7 Notion tools: search/read (untrusted), approved append (ADR-0021) | 2026-10-29 06:14, `710ba88` | merged |
| [#41](https://github.com/devampatel03/Nimbus/pull/41) | P3.9 commitment ledger wired to chat (ADR-0022) | 2026-10-29 07:28, `18151ab` | merged |
| [#42](https://github.com/devampatel03/Nimbus/pull/42) | P3.9 review fixes: card cap, Reject recorded, re-ask stale | 2026-10-29 13:19, `1a8fc55` | merged |
| [#43](https://github.com/devampatel03/Nimbus/pull/43) | P3.0 per-turn quarantine budget; runtime wiring of Quarantine and Google/Notion groups | 2026-10-29 14:12, `f8983f5` | merged |
| [#44](https://github.com/devampatel03/Nimbus/pull/44) | P3.0 pre-wiring follow-ups: P3.4 auth, #38 offer filter, #39 LOWs | 2026-10-29 14:35, `a1614e8` | merged |

Not shipped: **P3.6 Gmail** (search/read and draft create). Blocked on H16 (ADR-0007 Proposed). No Gmail tool exists in `src/`; `gmail.compose` is off by default (`GOOGLE_ALLOW_GMAIL_COMPOSE`).

Other W3 task status: P3.1 through P3.9 except P3.6 are merged. The P3.3 branch `task/P3.3-scheduler` was squashed into #30 (`0b1a565`). The tree is identical to the remote head `dc1cfa0`, which is not an ancestor of `pivot`. `task/P3.3b-lane-default` (`b57b781`, migration `e7b3a9c52d84`, fails 6 protected tests) is parked behind H23 and is not merged.

## 2. Gate numbers (command output, 2026-10-29, this worktree)

| Gate | Command | Result | Threshold / target |
|---|---|---|---|
| Non-adversarial (CI test job, includes unit, integration and invariants) | `uv run pytest --ignore=tests/adversarial -q` | **3280 passed**, 5 skipped, 0 failed (281 s) | `tests_must_pass: true`: met |
| Adversarial | `uv run pytest tests/adversarial -q` | **681 passed**, 0 failed (36 s) | `redteam_min_cases: 100`: this counts pytest items, not red-team cases; the 100-case corpus is P4.7 |
| Lint | `uv run ruff check .` | All checks passed | `lint_clean: true`: met |
| Types | `uv run mypy src` | Success: no issues in **139** source files | `types_clean: true`: met |

Changes from W2 (W2 gate: 1341 non-adversarial, 62 adversarial, 93 mypy files): the non-adversarial count is now 3280 and the adversarial item count is 681. The W2 split (unit 1090, integration 151, invariants 100) was not re-run as separate commands at W3, so only totals are reported here.

Not measured at W3: coverage (`min_coverage_security_policy` 0.95 and `min_coverage_changed_lines` 0.80). The W2 coverage run covered the W2 packages only. The per-PR coverage figures in section 1 come from STATE and were not re-measured.

Runtime and cost numbers: none measured. No live model call, no live Google, Notion or Telegram call, no LoCoMo re-run (stub 5.58%, ADR-0018, unchanged).

## 3. Decisions (this wave)

- **ADR-0019** (Google egress hosts) Accepted (P3.4). Refined by addenda for Calendar (`calendar.events` path rule, #37) and Drive (`deny_drive_write`, #36). R13 tightened, not widened.
- **ADR-0020** (approval implementation) status is still **Proposed** in the file, while P3.2 is merged (#35). Acceptance is pending.
- **ADR-0021** (Notion egress paths) **Proposed**; needs sign-off (H26). R13 narrowed to four Notion calls.
- **ADR-0022** (commitment ledger) **Proposed**; needs sign-off (H27). New append-only `commitment_ledger` (migration `a9d4c6e2f801`) with DB trigger `nimbus_ledger_guard`. A `confirmed` row is refused without a consumed `commitment`/`confirm` approval naming that candidate.
- **ADR-0010** (quarantine threshold) still **Proposed** (H18). Interim default in force: quarantine every untrusted field, no bypass.
- **ADR-0007** (Gmail scope) still **Proposed** (H16). P3.6 blocked.
- Pivot-internal decisions (no ADR): `PolicyEngine` replaces `DenyAllWritesPolicy` in the runtime (the class is still exported and used by adversarial tests); creates stay denied under `PENDING_IDEMPOTENCY`; Calendar and Notion show the model opaque handles, not provider ids; per-turn quarantine budget default 10, range 0-25 (P3.0 MED-D); google and notion tool groups are bound only when credentials exist (`src/app/integrations.py`).
- Next ADR number: **0023**.

## 4. Documentation changes in this PR

- `CLAUDE.md`: Current phase, Architecture quick reference, INV-5 (real policy engine and sealed approvals, no longer `DenyAllWritesPolicy`), INV-6 (built), R8 (commitment trigger), Known violations (cost gap), ADR next number 0023, migration head `a9d4c6e2f801` (checked with `uv run alembic heads`).
- `docs/pivot/STATE.md`: P3.0 to P3.9 rows (PR numbers, SHAs, open follow-ups), W3 gate numbers, Blockers, Next actions, branches section.
- `MEMORY.md`: append-only W3 section, plus a dated correction to the P3.5 entry. The approval card label is now `Target (from the service, UNTRUSTED)` (`src/approvals/card.py:78`), not `Target (from Google, UNTRUSTED)`.
- `docs/pivot/reports/KPI.md`: W3 section added on top. The W2 and W1 sections are kept as history.
- Not touched: `quality/thresholds.yaml` (no threshold changed), `personality/character_card.md`, code, `tests/**`.

## 5. Open risks

1. **Cost gap (staging-blocking).** Projected chat plus extraction cost is about 4,400 to 5,550 INR/month at placeholder prices (`src/llm/settings.py:35`, `*` at 250/1000 INR per Mtok), against `cost.projected_monthly_inr_max` 1500 (`quality/thresholds.yaml:72`). The figure comes from the STATE P3.3 row. Real prices must be at or below about 65 INR in and 270 INR out per Mtok, and the cap at most 100 INR/day alone allows about 3,000 INR/month. Owner: P5.5, re-project at the bake-off. `MAX_SPEND_SHARE` (`src/scheduler/memory_extract.py:90`) is a constant, not a setting.
2. **Superuser app DB role: HIGH (INV-3, R7) until P6.1/P6.2.** Unchanged. No waiver is recorded. No live run with production credentials until it is waived or fixed.
3. **Approvals are off by default.** They are ON only with `APPROVAL_HMAC_KEY` (H24). Approval-gated writes stay denied without it.
4. **No live run.** The bot, Google, Notion and the model were never called live. Every tool path is proven on fakes and recorded fixtures only.
5. **Extraction quality unmeasured.** `memory_extract` and commitment detection are tested with FakeLLM only. Real-model precision and recall are unknown.
6. **Untrusted commitment route unbuilt.** Commitments come only from the user's own (trusted) messages. Candidates from untrusted text (email, Drive, Notion) are not built. The P3.0 MED-D precondition is met, so this is a feature gap, not a budget gap.
7. **No chat path for `set_due`, `complete` or `cancel`.** The store methods exist (`src/memory/commitments.py`), but no chat entry point calls them. Commitments can be created and confirmed, not completed in chat.
8. **Lane server default still present.** `graph_edge`, `commitment` and `decision` keep `server_default 'trusted'` (`src/memory/schemas/graph.py:100`, `commitment.py:56`, `decision.py:51`). Removal is H23 (protected tests). A writer that forgets the lane labels content trusted until then.
9. **Disabled tools still offered.** `tasks_create` and `reminders_create` are offered while disabled (protected test pin, H29). `PolicyEngine.offerable` hides tools with no rule or a disabled scope, but not these.
10. **Approval-card limits.** Look-alike letters are not flagged. Callbacks queue behind turns for up to 180 s. An approved action's result is not passed back to the model. `pending_action.payload` is plaintext with no purge (the append-only trigger blocks deletion; a migration or ADR is needed).
11. **Dead token not persisted across restart** (P3.4 follow-up). `.env.example` lacks the `GOOGLE_*` and `NOTION_TOKEN` names (H28).
12. **Trusted-lane memory and the memory write path.** `record_event` to chunk to `memory_extract` is now wired (P3.3). Graph-edge promotion from untrusted sources is still unbuilt (P4.6).
13. **Carry-over from W2, unchanged:** spend cap default 200 INR with no upper bound; no alert hook wired (`src/app/runtime.py`); kill switch is a `system_flag` row, no `/panic`; `VLLMClient()` R4 latent path; R10 not wired (no stances or traits passed to the orchestrator); character card still a placeholder (H1); NOTICE cleanup; Apache AGE text in NOTICE.
14. **Process.** P3.3 was pushed as a branch and squashed into #30 late. Several PRs were fixed over multiple rounds. Calendar, Drive and Notion tools have LOW follow-ups still open (STATE P3.5 and P3.8 rows).

## 6. Human items at the W3 gate

- **H16** (Gmail scope, ADR-0007): open. Blocks P3.6. Gmail is not built.
- **H18** (ADR-0010, quarantine every untrusted field): open. Interim default in force.
- **H23** (approve two protected-test edits for the lane server default): open. Blocks P3.3b.
- **H24** (`APPROVAL_HMAC_KEY` and TOTP secret, added to `.env.example` by hand): open. Approvals stay off without them.
- **H25** (Drive grant needs `drive.readonly`): open; code is wired (#43). Live needs H4.
- **H26** (ADR-0021 sign-off, `NOTION_TOKEN`): open. Live needs H5.
- **H27** (ADR-0022 sign-off): open.
- **H28** (`.env.example` names for Google and Notion, `APP_QUARANTINE_CALLS_PER_TURN`): open, by hand.
- **H29** (protected test for disabled-tool offers): open.
- **H1** (character card), **H2** (Telegram token), **H3** (model key, billing), **H4** (Google consent, In production), **H5** (Notion token), **H6** (Oracle VM), **H12** (`.claude/settings.json`), **H19/H21** (`.env.example` and orphaned containers), **H22** (CODEOWNERS): open.
- **Waiver:** superuser app DB role. Human decision. No waiver recorded.
- **Staging with a real key:** real price table and cap at most 100 INR.

## 7. Honest limits

- Telegram is not end-to-end encrypted. Telegram, Oracle and the model provider can each see plaintext (Brief §8). Provider retention is outside our control.
- Oracle can reclaim the VM (Always Free, Brief §8).
- Prompt injection is bounded by architecture (quarantine, opaque handles, deterministic policy, sealed approvals), not eliminated. The quarantine model can itself be fooled; a fooled quarantine can only return schema-bounded data, not permissions (INV-5, INV-6). The red-team corpus is not yet 100 cases (P4.7).
- No privacy claim beyond this. No live run has been made.

## 8. Next wave (W4): gate verdict

Gate verdict: **GO with conditions** for W4 security work. Conditions:

1. Human decisions: H18 (ADR-0010), H16 (Gmail), H23; acceptance of ADR-0020 (H32), ADR-0021 (H26) and ADR-0022 (H27); H24 is only the approval env names. These gates block only the items that depend on them (P4.1 email category, P3.6, P3.3b), not the start of W4.
2. Start only unblocked work: P4.1 (email/invite/doc/recorded-speech red team; needs P3.6 for the email category, so the email category waits on H16), P4.3 (exfil: markdown/URL stripping), P4.4 (canary strings, outbound blocking), P4.5 (container egress), P4.6 (memory poisoning: untrusted never auto-promoted).
3. P4.7 (red-team corpus, protected) needs a new approval; the H14 approval covers only the TARGET_DESIGN Q1 scope.
4. Before any staging run with a real key: price table, cap at most 100 INR, alert hook, cost re-projection.

## 9. CLAUDE.md diff

The diff is in the PR (`git diff pivot -- CLAUDE.md`). Byte count before and after is reported in the PR description.
