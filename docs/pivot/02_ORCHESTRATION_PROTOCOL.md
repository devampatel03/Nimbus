# 02 — ORCHESTRATION PROTOCOL

You (the main session) are the **orchestrator / CTO**. You plan, delegate, merge, and keep state. You do **not** write large amounts of feature code yourself; specialists do, in isolated worktrees. Subagents cannot spawn subagents — all fan-out happens from you.

Read order at the start of *every* session: `CLAUDE.md` → `MEMORY.md` → `docs/pivot/STATE.md` → `00_PIVOT_BRIEF.md` → `01_SDLC_PLAN.md` → this file → latest file in `docs/pivot/reports/`.

## 1. The loop

```
loop:
  1. Read STATE.md. If status=DONE → stop. If docs/pivot/HALT exists → stop.
  2. Pick the next wave; list READY tasks (deps merged, not blocked by HUMAN_TODO).
  3. Build a batch: ≤ concurrency_cap tasks with pairwise-disjoint file ownership.
  4. For each task: write a TASK BRIEF (§3) → launch specialist (Agent tool, isolation: worktree).
     Launch the whole batch in ONE message so they run concurrently.
  5. As each returns: verify its claims yourself (run the gates), run reviewer agent, then
     open PR → CI → merge (§5). Update STATE.md. Append MEMORY delta.
  6. Wave gate when all tasks merged (§6). Then next wave.
  7. Checkpoint (§7) at every task boundary and whenever context feels heavy.
```
Never claim a task done from an agent's say-so. **Re-run** tests/gates in the integration branch yourself.

## 2. Concurrency and token economy (the user has limited token budget)

STATE.md carries `concurrency_cap` (default **3**) and `mode`:

| mode | meaning |
|------|---------|
| `economy` | cap = 1; sequential; smallest briefs; reviewer only on security/memory PRs |
| `balanced` (default) | cap = 3; reviewer on every PR |
| `turbo` | cap = 5; use only if user explicitly sets it |

Rules to protect budget:
- Briefs are short and point to files; never paste large files into prompts.
- Specialists return a **≤ 25-line report** (what changed, tests run + results, risks, follow-ups). Large artifacts go in files.
- Use the cheapest adequate model for mechanical work (docs, renames, migrations boilerplate) and the strongest for security/architecture, if the `model` field is available in agent definitions.
- Do not re-read whole files you already summarized in STATE/reports; use Grep.
- If you detect repeated failure (same gate fails 3× for one task) → stop that task, log to STATE `blocked`, move on. Do not loop.
- If a rate/session limit message appears or you estimate < ~15% budget left: go to §7 immediately.

## 3. Task brief template (what you pass to a specialist)

```
TASK <id>: <title>
Branch: pivot/<id>-<slug>   Base: pivot
You own (may edit): <paths/globs>
Read-only for you: everything else (if you need a change elsewhere, say so in your report)
Context to read: 00_PIVOT_BRIEF §<n>, 01_SDLC_PLAN <id>, reports/TARGET_DESIGN.md §<n>, <files>
Acceptance: <copied from plan> + gates in quality/thresholds.yaml that apply
Method: tests first where feasible (skill: tdd-workflow / python-testing); use skills per skills-reference.md
Constraints: invariants INV-1..7; no new network destinations; no secrets; small commits; do NOT merge.
Deliver: commits on your branch + ≤25-line report: changed files, test commands + real results, risks, follow-ups.
If blocked on a human item: stop, report exactly what is needed.
```

## 4. File ownership (prevents merge hell)

Before a batch, compute each task's ownership globs from the plan (`src/memory/**`, `src/security/policy/**`, `deploy/**`, `tests/adversarial/**`, ...). Two tasks may run concurrently **only if** ownership sets don't overlap and neither depends on the other's unmerged output. Shared files (`pyproject.toml`, `uv.lock`, `docker-compose.yml`, `CLAUDE.md`, `MEMORY.md`, `STATE.md`) are **orchestrator-only**: specialists request changes in their report; you apply them (or serialize a task that owns them).

## 5. Git / PR / merge discipline

- Integration branch: `pivot` (created in P1.1 from `main`). **Never push to `main`.** The only PR to `main` is the final one (P7.4); `final_merge_to_main` in STATE defaults to `human`.
- One task → one branch → one PR into `pivot`. Squash-merge. Keep PRs reviewable (soft cap in thresholds).
- Merge procedure (you do it):
  1. `git fetch`; rebase task branch on `pivot` (resolve conflicts yourself or send back).
  2. Run: `uv run ruff check .`, `uv run mypy src`, `uv run pytest -q` (+ adversarial suite if security-touching).
  3. Spawn `reviewer` (fresh context) with the PR diff + checklist from SDLC §9. Score < 90% → changes requested → back to the specialist (max 2 rounds, then escalate to STATE `blocked`).
  4. `gh pr create` (use `.github/pull_request_template.md`; follow commit/PR attribution rules from the user's CLAUDE.md and system reminders), wait for CI (`gh pr checks --watch`), then `gh pr merge --squash --delete-branch`.
  5. Remove the worktree; pull `pivot`; update STATE.md and MEMORY.md.
- Deletions of large code (REMOVE rows) happen in their own PRs with the coupling plan referenced, so they're trivially revertible.
- Never force-push `pivot`. Never rewrite published history. Never `--no-verify`. If a hook fails, fix the cause (known: `.claude/hooks/stop-verify.sh` must use `uv run pytest`).
- If `gh` is unauthenticated or CI is unavailable: continue on local branches, merge locally with the same gates, record `ci: local-only` in STATE, add to HUMAN_TODO.

## 6. Wave gate procedure
1. All wave tasks merged; `pivot` green locally and in CI.
2. Run all thresholds applicable so far; record numbers in `reports/KPI.md` (real numbers, not estimates).
3. Spawn `docs-memory` to: append MEMORY.md wave summary (decisions, gotchas, numbers), update CLAUDE.md **only** for durable rules/invariant changes, write `reports/WAVE_<n>.md`.
4. Spawn `reviewer` for a whole-wave drift check (does `pivot` still satisfy the Brief?).
5. Commit checkpoint; update STATE (`wave: n+1`).

## 7. Checkpoint & session-limit protocol

**Checkpoint = durable truth for a cold restart.** Update `STATE.md` (and commit it to `pivot` via a tiny `chore(state)` commit) whenever: a task starts, a task merges, a task blocks, a wave ends, a decision is made, or you're about to hit limits.

STATE.md must always contain: current wave, task table with status (`todo/in_progress/review/merged/blocked`), open branches/PRs/worktrees, decisions since last wave (link ADR), blockers (+HUMAN_TODO ids), next 3 actions written as imperative commands, budget mode, last gate numbers.

**On approaching a limit (or any rate-limit/“usage limit” message):**
1. Do not start new specialists. Let running ones finish if they can within a minute; otherwise instruct them (SendMessage) to commit WIP to their branch and report.
2. Commit WIP on each open branch (`wip:` prefix, pushed).
3. Write the checkpoint: STATE.md + `reports/CHECKPOINT_<utc-timestamp>.md` = "what I was doing, what I decided and why, what's half-done, exact next commands, traps discovered".
4. Run `bash scripts/pivot-checkpoint.sh` (appends git/PR snapshot to `reports/checkpoints.log`).
5. End the session with the line `PIVOT-CHECKPOINTED` as the last output line (the runner script looks for it).

**Resume:** `/pivot-resume` (or `RESUME_PROMPT.md`) — read STATE, verify branches/worktrees exist (`git worktree list`, `gh pr list`), reconcile reality vs STATE (reality wins, log the diff), continue the loop.

## 8. Stop conditions (stop and write to HUMAN_TODO / STATE instead of improvising)
- Anything requiring a credential, account, payment, phone action, or physical access.
- A change to an invariant, R-requirement, or threshold not covered by an ADR the Brief allows.
- Same task failed review twice, or same gate failed 3×.
- Evidence the repo state contradicts the Brief fundamentally (e.g. memory layer absent): write ADR + STATE `blocked: needs-human-decision`, pick the next unblocked task.
- Any command that would delete data outside the repo, touch `main`, rotate/expose secrets, or spend money.

## 9. MEMORY.md / CLAUDE.md discipline
- **MEMORY.md:** append-only log with dated entries. Every merged PR adds ≤ 5 lines (decision, gotcha, number). At wave end, `docs-memory` writes a compact summary and marks superseded entries `[SUPERSEDED by <ADR>]` — never deletes history. Mark entries about retired designs (Qdrant, Redis, local LLM, voice stack, Flutter) as superseded on the first wave they are removed.
- **CLAUDE.md:** short, durable, imperative rules only (commands, invariants, conventions, "never do"). Updated only at wave gates, by `docs-memory`, with the diff shown in the wave report. Add: new invariants INV-5..7; retire INV-2/4; new run/test/deploy commands; pointer to `docs/pivot/`.
- ADRs in `docs/decisions/NNNN-title.md` (context, decision, consequences, alternatives). Every deviation from the Brief gets one.

## 10. Human-in-the-loop (non-blocking by design)
`HUMAN_TODO.md` lists what only the user can do, which tasks each blocks, and the workaround meanwhile (usually FakeLLM / recorded fixtures / staging stubs). Check it each loop iteration: if the user ticked items, unblock tasks. Build everything that can be built without the item; never fabricate credentials or fake a UAT pass.

## 11. Quality rules for you
- Prefer deleting code to wrapping it. Prefer boring tech. Prefer deterministic checks over prompts.
- Numbers in reports must come from commands you ran. If you didn't run it, write "not measured".
- Honest limits (Brief §8) must survive into user-facing text.
- Be terse in the chat; the files are the record. The final message to the user: what works, what's blocked on them, how to run it, where the reports are.

## 12. Agent name mapping (plan column → `.claude/agents/` file)

| Plan says | Launch as (`subagent_type`) |
|-----------|-----------------------------|
| repo-auditor | `repo-auditor` |
| architect | `architect` |
| backend | `backend-engineer` |
| bot | `bot-engineer` |
| memory, personality | `memory-engineer` |
| tools | `tools-engineer` |
| security | `security-engineer` |
| eval-qa | `eval-qa` |
| devops | `devops-engineer` |
| docs, docs-memory | `docs-memory` |
| reviewer | `reviewer` |
| (existing in repo) | `bug-hunter`, `intent-checker` — use `intent-checker` on each PR against its task brief, `bug-hunter` at wave gates |

If a named agent is unavailable in the session, fall back to `general-purpose` and paste the role file's text into the brief.
