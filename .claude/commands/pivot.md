---
description: Kick off the Nimbus Lite architecture pivot — audit the repo, plan, and run the multi-agent SDLC to a working result
argument-hint: "[economy|balanced|turbo] (optional, default balanced)"
---

We are **pivoting Nimbus** to the "Nimbus Lite" architecture (Telegram-first, Oracle A1 VM, Postgres+pgvector, one paid no-training model via an OpenAI-compatible adapter, least-privilege Google/Notion tools, architecture-based prompt-injection defense). You are the **orchestrator**. Mode argument: `$ARGUMENTS` (blank = `balanced`).

Do this now, without asking me questions:

1. Read `CLAUDE.md`, `MEMORY.md`, then `docs/pivot/00_PIVOT_BRIEF.md`, `01_SDLC_PLAN.md`, `02_ORCHESTRATION_PROTOCOL.md`, `STATE.md`, `HUMAN_TODO.md`, and `quality/thresholds.yaml`. Also read `~/.claude/references/skills-reference.md` if present and use it to pick skills for each task.
2. Set `mode` / `concurrency_cap` in `docs/pivot/STATE.md` from my argument (economy=1, balanced=3, turbo=5). Set `status: RUNNING`.
3. Run `python scripts/envcheck.py` and record the result; note anything that blocks work in `HUMAN_TODO.md` (do not stop for it).
4. Create the integration branch `pivot` from the current default branch (commit any pending unrelated harness changes separately or leave them untouched — do not lose them).
5. Execute **W0 (Audit & Reconcile)** exactly as in the SDLC plan using the `repo-auditor` and `architect` agents (parallel where file ownership allows). Inspect the real repo: decide what to **discard, modify, remove, create**, and how the pieces connect. Evidence over assumptions — the Brief's disposition table is only a hypothesis.
6. Refine the task table in `STATE.md` (P0.5), open the audit as a PR into `pivot`, then continue through W1 → W7 following the orchestration protocol: specialists in isolated worktrees, throttled parallelism, PR → CI → reviewer → squash-merge into `pivot`, MEMORY.md/CLAUDE.md updates at wave gates, checkpoint at every task boundary and before any usage limit.
7. Never push to `main`. The final PR `pivot → main` is left for me.
8. If you hit a session/usage limit, follow protocol §7 and end with the exact last line `PIVOT-CHECKPOINTED`. When everything is complete, end with `PIVOT-DONE`.

Start by telling me in one sentence what you're doing, then begin.
