# Nimbus Pivot Kit — README FIRST

**What this is:** everything a fresh Claude Code session needs to pivot Nimbus to *Nimbus Lite* (Telegram-first · Oracle A1 · Postgres+pgvector · one paid no-training model · least-privilege tools · architectural prompt-injection defense) and then build, test, harden, deploy and document it — as a team of specialist agents, with PRs, checkpoints and memory updates — while you're away.

## 0. The 60-second version
1. Copy this kit into the **root of the Nimbus repo** (`C:\DEVAM\MY_PROJECTS\Nimbus`) — see §2 for the one file you must *merge*, not overwrite.
2. Commit it: `git add -A && git commit -m "chore: add pivot kit"` (on a clean branch; leave your unrelated harness changes as they are).
3. Open a **new** Claude Code session in the repo and send:
   > **We are moving with the Nimbus Lite architecture — pivoting. Run `/pivot`.**
4. Walk away. Optionally run the unattended runner (§4) so it relaunches itself after session limits.
5. Come back to: `docs/pivot/STATE.md` (where it is), `docs/pivot/HUMAN_TODO.md` (what only you can do), `docs/pivot/reports/` (evidence), and one final PR `pivot → main` for you to review.

If slash commands don't load for any reason, paste the contents of `docs/pivot/RESUME_PROMPT.md` as your first message instead — it's the same instruction.

## 1. What's in the kit

| Path | Purpose |
|------|---------|
| `docs/pivot/00_PIVOT_BRIEF.md` | Normative decision: architecture, requirements R1–R16, invariants, repo-disposition **hypotheses**, honest limits |
| `docs/pivot/01_SDLC_PLAN.md` | Waves W0–W7, ~50 tasks with owners + acceptance, test strategy, UAT, production-readiness review, KPIs, risks, grading checklists |
| `docs/pivot/02_ORCHESTRATION_PROTOCOL.md` | How the orchestrator runs specialists, parallelism rules, PR/merge discipline, checkpoint & session-limit protocol, memory/CLAUDE.md rules |
| `docs/pivot/STATE.md` | Live state file (the agents' durable memory across sessions) |
| `docs/pivot/HUMAN_TODO.md` | Things only you can do, what each blocks, and the interim workaround |
| `docs/pivot/RESUME_PROMPT.md` | Cold-start prompt used by `/pivot-resume` and the runner |
| `quality/thresholds.yaml` | Machine-checkable gates (red-team = 0 successes, etc.) |
| `.claude/commands/{pivot,pivot-resume}.md` | Slash commands |
| `.claude/agents/*.md` | 11 specialists: repo-auditor, architect, backend-engineer, bot-engineer, memory-engineer, tools-engineer, security-engineer, eval-qa, devops-engineer, docs-memory, reviewer |
| `.claude/settings.pivot.snippet.json` | Permission allow/deny list + PreCompact checkpoint hook to merge |
| `.github/pull_request_template.md` | PR template used by agents |
| `scripts/envcheck.py` | Reports tools/credentials present (never prints values) |
| `scripts/pivot-checkpoint.sh` | Git/PR snapshot appended to `reports/checkpoints.log` |
| `scripts/pivot-runner.ps1` | Windows loop that relaunches the session after limits until DONE |

## 2. Install (5 minutes)

```powershell
# from the kit folder, copy into the repo root (/XC /XN /XO = never overwrite files that already exist)
robocopy . C:\DEVAM\MY_PROJECTS\Nimbus /E /XC /XN /XO /XF README_FIRST.md
```
(or just drag-and-drop the folders; if Windows asks about overwriting, choose **skip** for anything that already exists.)

**Merge, don't overwrite:** `.claude/settings.pivot.snippet.json` → into your existing `.claude/settings.json` (add its `permissions.allow`/`deny` entries and the `PreCompact` hook to what you already have). Keep your current hooks. If you'd rather not touch settings, skip it and run interactively — the agents will then prompt for each permission.

**Pre-flight (do these once):**
```powershell
gh auth status                    # must be logged in; repo must have Actions enabled
python scripts\envcheck.py        # shows what's missing; missing optional items are fine
git switch -c pivot-kit-install ; git add -A ; git commit -m "chore: add pivot kit"
```
Commit/merge that install branch to your default branch first (or let the orchestrator branch from it) so `pivot` starts from a state that includes the kit.

## 3. What the agents will do (and won't)

**Will:** audit the repo with evidence → decide discard/modify/remove/create → refine the task list → build W1…W7 with specialists in isolated git worktrees (max 3 at once by default) → every task is a PR into a long-lived `pivot` branch → CI + independent reviewer → squash-merge → update `MEMORY.md` per PR and `CLAUDE.md` at wave gates → checkpoint constantly → open the final PR.

**Won't:** push to `main`; add Gmail send scope; touch your character card; invent API keys or fake UAT passes; spend money; ask you questions mid-run (blockers go to `HUMAN_TODO.md` and work continues with fakes).

**You will need to supply (see `HUMAN_TODO.md`):** the hand-written character card (H1) · Telegram bot token(s) + your user ID (H2) · a billing-enabled model API key for the bake-off (H3) · Google OAuth app "In production" (H4) · Notion token (H5) · Oracle VM (H6) · backup target (H7) · uptime ping (H8) · UAT items marked `[human]` (H9). Everything *buildable* without them gets built with fakes first, so you can supply them as the run progresses.

## 4. Token budget & parallelism (important — you told us this matters)

Parallel agents burn tokens faster. The protocol therefore throttles instead of going wide:

- Modes (set via `/pivot economy|balanced|turbo`, or edit `STATE.md`): **economy** = 1 agent at a time · **balanced** (default) = up to 3 with disjoint file ownership · **turbo** = 5.
- Specialist reports are capped at ~25 lines; big outputs go to files, not chat.
- If you want the strictest budget behavior, start with `/pivot economy`: same plan, strictly sequential, reviewer only on security/memory PRs.
- Hitting a limit is **expected**, not a failure: the session writes a checkpoint (`STATE.md` + `reports/CHECKPOINT_*.md`), commits WIP, prints `PIVOT-CHECKPOINTED`, and stops. Resume with `/pivot-resume`, or let the runner do it:

```powershell
.\scripts\pivot-runner.ps1                        # balanced
.\scripts\pivot-runner.ps1 -Mode economy -SleepMinutes 45
New-Item docs\pivot\HALT                          # emergency stop (ends after current session)
```
The runner uses `--permission-mode acceptEdits` plus your allow-list. `-Unsafe` (skip all permission prompts) exists but should only ever be used inside a disposable VM or container, never on your main machine with live credentials. Runner logs: `docs\pivot\reports\runner\`.

*Verify once:* flag names (`-p`, `--permission-mode`, `--output-format`) match your installed Claude CLI version (`claude --help`). If your plan's usage windows reset on a schedule, set `-SleepMinutes` to match.

## 5. How you stay in control without babysitting
- **Single review point:** the final `pivot → main` PR. Everything before that lives on `pivot`.
- **Evidence:** every wave ends with `reports/WAVE_n.md` (real numbers vs `thresholds.yaml`).
- **Brake:** create `docs/pivot/HALT`. **Reset a decision:** edit `STATE.md`/an ADR and say so in the next session.
- **Audit trail:** every PR has a reviewer verdict; `MEMORY.md` has a dated line per decision.

## 6. Honest expectations
- "Final working output" = a bot that runs locally against fakes and in staging once you supply H2/H3/H6; true production needs your keys, VM, and the 72 h soak. The agents get everything to that boundary and tell you exactly what remains.
- Quality numbers (LoCoMo, personality evals) need a real model key; before H3 they validate plumbing only and are labeled so.
- A single long autonomous run can drift. The reviewer, wave gates, and `thresholds.yaml` exist for that; skim `reports/WAVE_n.md` after each wave if you can.
- Privacy is "reasonable," not absolute: Telegram, Oracle and the model provider can see plaintext. The PWA/TEE lane is the upgrade path (deferred).
