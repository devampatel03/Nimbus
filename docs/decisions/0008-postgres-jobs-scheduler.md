# 0008 — Job queue and scheduler: one Postgres `job` table with `FOR UPDATE SKIP LOCKED`; drop APScheduler and arq

- Status: Accepted. Resolves Brief §7 open decision "Job queue" (default confirmed).

## Context
- `arq` is declared and never imported (AUDIT_DISPOSITION §0.9).
- `reminders.py` uses APScheduler with an in-memory jobstore, so reminders are lost on restart (§0.12), which violates R1 and R11.
- Turn processing, post-turn extraction, reminders and proactive messages all need durable, at-least-once execution in one process.

## Decision
One `job` table with columns: `id`, `kind`, `run_at timestamptz`, `payload jsonb`, `dedupe_key unique`, `rrule text null`, `proactive bool`, `status`, `attempts`, `last_error` (sanitised), `locked_at`.

A runner loop inside the app process works like this:
- It ticks every 5 s (or earlier, woken by `LISTEN/NOTIFY` on insert).
- Each tick claims up to 20 due jobs with `SELECT ... FOR UPDATE SKIP LOCKED` and dispatches each to a registered handler by `kind`.
- On success it marks the job done, or schedules the next RRULE occurrence (`python-dateutil`).
- On failure it backs off. After 5 attempts the job is marked dead and the user is alerted.
- On start-up, everything with `run_at <= now()` runs (catch-up). Reminders fired late are labelled as late.
- Time is UTC in storage. The `Asia/Kolkata` conversion happens only at parse and render time (`core/timeparse.py`).

## Consequences
- No new services, and job state is included in the nightly `pg_dump` (R14).
- 5 s polling cost is negligible for a single user. Delivery latency is ≤ 5 s + send time (R11 ≤ 60 s).
- A fake `Clock` plus `run_due(now)` makes every scheduler test deterministic.

## Alternatives
- **APScheduler with an SQLAlchemy jobstore.** Rejected: the jobstore is sync-driver only, it is a second scheduling model next to the turn queue, and its catch-up semantics (`misfire_grace_time`) are harder to test.
- **Redis/arq.** Rejected: Redis is removed (Brief).
- **pg_cron.** Rejected: it puts the logic in the DB, needs an extension, and is harder to fake in tests.
