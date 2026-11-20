# ENV_CHECKLIST: environment variable names (no values)

Date: 2026-11-03 (W5 follow-up). For the human filling `.env` / `.env.example` (agents may
not edit `.env.example`). Derived from the settings classes in code: `config/settings.py`,
`src/*/settings.py`, `src/integrations/google/auth.py` (`GoogleOAuthSettings`), and the
two raw `os.environ` reads (`src/db/crypto.py`, `src/personality/agent_character.py`).
Every class reads `.env` from the working directory; real env vars win over `.env`.
**Never put a value in this file or in any committed file (INV-7).**

Column "First run" = needed for a first local run against a free-tier key:
**REQ** required, **REC** recommended (a feature is off or weaker without it), blank = optional.
No `PROACTIVE_*` variables exist in code yet (proactive jobs have no settings class).

## Required or recommended for the first run

| Name | Purpose | First run | Default |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot API token (secret; format-checked) | REQ | none (startup fails) |
| `TELEGRAM_ALLOWED_USER_ID` | The one Telegram user the bot serves (R1-R3) | REQ | none (startup fails) |
| `LLM_BASE_URL` | OpenAI-compatible endpoint; must be https:443. Its host is the only `Service.LLM` egress host (also read by `src/net` as `NET_LLM_BASE_URL`) | REQ | `http://localhost:8000/v1` for `src/llm`; `src/net` then has no LLM host (all LLM egress denied) |
| `LLM_API_KEY` | Provider key (secret, scrubbed) | REQ | `not-needed` placeholder |
| `LLM_MODEL` | Chat model id | REQ | `Qwen/Qwen2.5-7B-Instruct` |
| `LLM_PRICE_IN_PER_MTOK_INR`, `LLM_PRICE_OUT_PER_MTOK_INR` | Price of `LLM_MODEL` (set both). Free key: `0.0001` (0 refused); startup then WARNs the INR cap is no bound | REQ | unset => `*` placeholder 250 / 1000 |
| `LLM_RPM_LIMIT`, `LLM_DAILY_REQUEST_LIMIT` | Request limits: the real bound on a free key (set below the key's quota; COST_MODEL s5) | REQ (free key) | 120, 2000 |
| `LLM_REASONING_EFFORT` | `none`/`minimal`/`low`/`medium`/`high`; `minimal` in the cheap profile | REC | unset (provider default) |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | DB connection (no prefix; also used by `docker-compose.yml`). Password is a secret | REQ (defaults match compose) | `localhost`, 5432, `nimbus`, `nimbus`, `nimbus` |
| `NIMBUS_DATA_KEY` | AES-256-GCM column key, base64 of exactly 32 bytes (secret). Needed for `LLM_STORE_PAYLOAD=true` and for Google tokens | REC | unset (those features off) |
| `APPROVAL_HMAC_KEY` | Signs approval `action_id`s, >= 32 bytes (secret). Unset = approvals off, every `require_approval` is a denial | REC (no writes without it) | unset |
| `APPROVAL_TOTP_SECRET` | Base32 TOTP secret, >= 128 bits (secret). Unset = destructive / external actions can never be approved | REC | unset |
| `CANARY_SEED` | >= 32 chars (secret); stable canaries across restarts, enables `canary-sweep` | REC | unset (random per process) |
| `APP_ENV` | `development` or `production` | REQ for fakes / card dev mode | `production` |
| `NIMBUS_CARD_DEV_MODE` | `1` = bypass the character-card approval pin (refused unless `APP_ENV=development`) | only without an approved card | unset |

Character card: not an env var. The path is fixed, `personality/character_card.md` plus
its `.sha256` pin (R9, ADR-0015); startup refuses an unapproved card unless dev mode.

## LLM_ (src/llm/settings.py), remaining

| Name | Purpose | Default |
|---|---|---|
| `LLM_MODEL_CHEAP` | Cheaper model for quarantine / extraction, same host | unset (uses `LLM_MODEL`) |
| `LLM_CHEAP_PRICE_IN_PER_MTOK_INR`, `LLM_CHEAP_PRICE_OUT_PER_MTOK_INR` | Price of `LLM_MODEL_CHEAP` (both or neither) | unset |
| `LLM_PRICE_TABLE` | JSON price table; `*` prices unlisted models | `{"*": 250 / 1000}` |
| `LLM_DAILY_SPEND_CAP_INR` | Daily INR cap (R5), <= 100 enforced (ADR-0025) | 50 |
| `LLM_TIMEOUT_SECONDS` | Provider call timeout (<= 300) | 60 |
| `LLM_MAX_RETRIES`, `LLM_BACKOFF_BASE_S`, `LLM_BACKOFF_MAX_S` | Retry policy | 3, 0.5, 8.0 |
| `LLM_BACKGROUND_SHARE` | Share of request limits background work may use | 0.5 |
| `LLM_RATE_LIMIT_MAX_WAIT_S` | How long chat waits for a slot before the "busy" reply | 40 |
| `LLM_MAX_OUTPUT_TOKENS_TRUSTED`, `_QUARANTINE`, `_SYSTEM` | Per-lane output caps | 600, 800, 400 |
| `LLM_STORE_PAYLOAD` | Store the redacted request encrypted in `llm_audit` (needs `NIMBUS_DATA_KEY`) | false |
| `LLM_PAYLOAD_RETENTION_DAYS` | Retention of stored payloads | 14 |
| `LLM_CANARIES` | Extra canary strings (JSON list; secret) | empty |

## APPROVAL_ (src/approvals/settings.py), remaining

| Name | Purpose | Default |
|---|---|---|
| `APPROVAL_TTL_S` | Card lifetime (60-900) | 600 |
| `APPROVAL_STEP_UP_WINDOW_S` | TOTP entry window after the tap | 180 |
| `APPROVAL_MAX_OPEN`, `APPROVAL_MAX_OPEN_COMMITMENT` | Open cards at once (tools / commitments) | 3, 1 |
| `APPROVAL_TOTP_ATTEMPTS_PER_ACTION`, `APPROVAL_TOTP_LOCKOUT_FAILURES`, `APPROVAL_TOTP_LOCKOUT_WINDOW_S` | TOTP brute-force bounds | 3, 5, 900 |
| `APPROVAL_DISPLAY_UTC_OFFSET_MINUTES` | Card clock (IST) | 330 |

## CANARY_ (src/canary/settings.py), remaining

| Name | Purpose | Default |
|---|---|---|
| `CANARY_ALERT_TELEGRAM` | Fixed-text alert to the user on a canary hit | true |
| `CANARY_ALERT_COOLDOWN_S` | At most one alert per window (>= 30) | 600 |

## GOOGLE_ (GoogleOAuthSettings) and NOTION_

| Name | Purpose | Required for | Default |
|---|---|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | OAuth client (secrets); then run `scripts/google_oauth.py` (H4) | Calendar / Drive tools (also `NIMBUS_DATA_KEY`) | unset (Google off) |
| `GOOGLE_ALLOW_GMAIL_COMPOSE` | Strict `true`/`false`; widens the scope ceiling (H16 pending) | never, until H16 | false |
| `GOOGLE_MAX_RETRIES`, `GOOGLE_BACKOFF_BASE_S`, `GOOGLE_BACKOFF_MAX_S`, `GOOGLE_REQUEST_TIMEOUT_S` | Google call tuning | | 2, 0.5, 8.0, 15 |
| `NOTION_TOKEN` | Internal integration secret (H5) | Notion tools | unset (Notion off) |

## TELEGRAM_ (src/gateway/settings.py), remaining

| Name | Purpose | Default |
|---|---|---|
| `TELEGRAM_POLL_TIMEOUT_S` | Long-poll timeout (1-50) | 20 |
| `TELEGRAM_MAX_VOICE_BYTES`, `TELEGRAM_MAX_DOCUMENT_BYTES` | Inbound file caps | 10 MB, 1 MB |
| `TELEGRAM_PER_CHAT_INTERVAL_S`, `TELEGRAM_GLOBAL_INTERVAL_S` | Send pacing | 1.0, 1/30 |
| `TELEGRAM_MAX_429_RETRIES`, `TELEGRAM_MAX_RETRY_AFTER_S` | 429 handling | 3, 120 |
| `TELEGRAM_NOTICE_UTC_OFFSET_MINUTES` | Clock in the failure notice | 330 |
| `TELEGRAM_REPLY_URL_ALLOWLIST` | JSON list of hosts a reply may link to (display only) | empty |

## NET_ (src/net/settings.py)

| Name | Purpose | Default |
|---|---|---|
| `NET_LLM_BASE_URL` | Alias of `LLM_BASE_URL` for the egress host | `LLM_BASE_URL` |
| `NET_HEALTHCHECK_HOST`, `NET_BACKUP_HOST` | Optional extra egress hosts (off unless set) | unset |
| `NET_ENABLED_SERVICES` | JSON list narrowing the enabled `Service`s (can only disable) | all with a known host |
| `NET_CONNECT_TIMEOUT_S`, `NET_READ_TIMEOUT_S`, `NET_WRITE_TIMEOUT_S`, `NET_POOL_TIMEOUT_S`, `NET_TOTAL_TIMEOUT_S` | Transport timeouts | 5, 30, 30, 5, 60 |
| `NET_MAX_RESPONSE_BYTES`, `NET_MAX_REDIRECTS` | Response bounds | 10 MB, 3 |

## APP_ (src/app/settings.py), remaining

| Name | Purpose | Default |
|---|---|---|
| `APP_FAKE_BACKENDS` | In-process fakes (needs `APP_ENV=development`) | false |
| `APP_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | INFO |
| `APP_EMBEDDER` | `local` (needs `uv sync --extra local-embed`) or `api` | local |
| `APP_HISTORY_MESSAGES`, `APP_MEMORY_TOP_K`, `APP_MEMORY_ITEM_CHARS`, `APP_QUARANTINE_CALLS_PER_TURN` | Prompt-size cost levers (COST_MODEL s4) | 20, 5, 1200, 10 |
| `APP_EXTRACT_MODE`, `APP_EXTRACT_MIN_CHARS`, `APP_EXTRACT_MAX_SPEND_SHARE` | Memory extraction cost levers | all, 40, 0.30 |
| `APP_HEARTBEAT_PATH`, `APP_HEARTBEAT_STALE_S` | Health check file | per-user state dir, 180 |
| `APP_BACKOFF_*`, `APP_SHUTDOWN_GRACE_S`, `APP_RECOVER_*`, `APP_SCHEDULER_TICK_S`, `APP_JOB_*`, `APP_EXTRACT_PAUSE_S`, `APP_TURN_QUEUE_SIZE`, `APP_DB_STATEMENT_TIMEOUT_S` | Loop and job tuning (see `src/app/settings.py`) | as in code |

## POLICY_ (src/app/policy_settings.py, P3.3-enable). Human-edited only

| Name | Purpose | Default |
|---|---|---|
| `POLICY_ENABLE_CREATES` | Lets `tasks_create` and `reminders_create` through to the ADR-0009 matrix (clean turn: allowed; tainted turn: approval). Strict `true`/`false`; anything else refuses startup. Deletes are unaffected. Boot logs one INFO line with the state | false |

## PROACTIVE_ (src/scheduler/settings.py, P5.1). None required for the first run

| Name | Purpose | Default |
|---|---|---|
| `PROACTIVE_ENABLED` | Master switch for briefs, check-ins, nudges, weekly review | true |
| `PROACTIVE_BUDGET_PER_DAY` | Interruption budget per IST day across all kinds (0-6, refused above 6) | 6 |
| `PROACTIVE_QUIET_START`, `PROACTIVE_QUIET_END` | Quiet hours, IST `HH:MM` (equal = none) | 22:00, 08:00 |
| `PROACTIVE_MORNING_ENABLED`, `PROACTIVE_MORNING_AT`, `PROACTIVE_MORNING_VALID_H` | Morning brief on/off, time, how late it may still be sent | true, 08:30, 6 |
| `PROACTIVE_EVENING_ENABLED`, `PROACTIVE_EVENING_AT`, `PROACTIVE_EVENING_VALID_H` | Evening check-in | true, 20:30, 1.5 |
| `PROACTIVE_WEEKLY_ENABLED`, `PROACTIVE_WEEKLY_WEEKDAY`, `PROACTIVE_WEEKLY_AT`, `PROACTIVE_WEEKLY_VALID_H`, `PROACTIVE_WEEKLY_CARDS_MAX` | Weekly review (Mon=0..Sun=6), memory approval cards per review (0-10) | true, 6, 18:00, 4, 3 |
| `PROACTIVE_NUDGES_ENABLED`, `PROACTIVE_NUDGE_LEAD_H`, `PROACTIVE_NUDGE_OVERDUE_DAYS_MAX`, `PROACTIVE_NUDGE_MAX_PER_DAY`, `PROACTIVE_NUDGE_SCAN_S`, `PROACTIVE_NUDGE_VALID_H` | Commitment nudges | true, 24, 3, 3, 600, 4 |
| `PROACTIVE_ITEMS_MAX`, `PROACTIVE_SKIP_EMPTY_BRIEF`, `PROACTIVE_SNOOZE_MAX_H` | Lines per section, skip an empty brief, longest `/snooze` | 5, true, 72 |

No model calls: every proactive message is a template (cost 0).

