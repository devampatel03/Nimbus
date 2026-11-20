# Runbook: first local run (Windows, PowerShell)

Goal: run Nimbus on your own PC with Docker Desktop Postgres, a free-tier LLM key and one Telegram bot, and see it answer. This is a **test run, not a deployment**. Never put production secrets or real personal data through it. Privacy stance as in Brief section 8: Telegram and the model provider see your messages in plaintext, and a free-tier provider may use them to improve its products (COST_MODEL.md section 1).

Names of every variable are in `docs/pivot/ENV_CHECKLIST.md`. **Values go only in your `.env` file, never in a chat, commit or log.** Agents do not read `.env`.

All commands run from the repo root in PowerShell. Use `uv run --extra dev ...` (a bare `uv run` can re-sync without the dev extra).

## 1. Create the Telegram bot and find your id

1. In Telegram open `@BotFather`, send `/newbot`, pick a name and a username ending in `bot`. Copy the token it prints (looks like `123456789:AA...`). That is `TELEGRAM_BOT_TOKEN`.
2. Your numeric user id (not your @username): message `@userinfobot` (or any "get my id" bot). That is `TELEGRAM_ALLOWED_USER_ID`. Only this user is served; every other sender is dropped (R1-R3).
3. Open your new bot in Telegram and press Start once, so it may message you.

## 2. Create `.env`

Create `.env` in the repo root (it is git-ignored) with these names. Fill the values yourself:

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_ID=
LLM_BASE_URL=            # https://<provider host>/<path>, https only, no port, no key in the URL
LLM_API_KEY=
LLM_MODEL=               # your provider's model id
LLM_PRICE_IN_PER_MTOK_INR=0.0001     # free key: tiny non-zero (0 is refused)
LLM_PRICE_OUT_PER_MTOK_INR=0.0001
LLM_RPM_LIMIT=            # below your key's requests/minute quota
LLM_DAILY_REQUEST_LIMIT=  # below your key's requests/day quota
APP_ENV=development
APP_EMBEDDER=api          # see step 3: the local embedder needs a big download
APP_EMBED_MODEL=          # an embedding model your provider serves via /embeddings
APP_EXTRACT_MODE=cue      # fewer memory-extraction calls: saves free-tier quota
```

Notes that are easy to miss (all verified in code):

- `LLM_BASE_URL`'s host becomes the only LLM egress host (default-deny allowlist, R13). Plain `http://` or `localhost` is refused.
- Free key: the INR cap bounds nothing at price 0.0001; **the request limits are the real bound**. Startup prints a WARNING about it. Do not leave `LLM_RPM_LIMIT` / `LLM_DAILY_REQUEST_LIMIT` at the paid defaults (120 / 2000).
- Postgres: `POSTGRES_*` defaults (`nimbus`/`nimbus`/`nimbus` on localhost:5432) match `docker-compose.yml`; set them only if you changed the container.
- `APPROVAL_HMAC_KEY` (>= 32 chars) switches approvals on; without it **approvals are OFF and every write tool is denied** (chat and reads still work). Skip it for a first chat test. To try it: `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`, paste into `.env` yourself. Optional `APPROVAL_TOTP_SECRET` (base32) is needed for destructive actions.
- `NIMBUS_DATA_KEY` is optional. **It is read from the process environment, not from `.env`** (as is the card switch below), so set it in the shell if you use it.
- Google and Notion variables are optional; leave them out.

## 3. Embedder: pick the cheap path

`APP_EMBEDDER` defaults to `local` (Qwen3-Embedding-0.6B via sentence-transformers). That needs `uv sync --extra dev --extra local-embed` (pulls torch, GBs), **and** the weights already in your Hugging Face cache, because the app runs with `HF_HUB_OFFLINE=1` and never downloads (INV-1). Without both, chat still works but memory writes and recall fail (logged as errors).

For a first run use `APP_EMBEDDER=api`: embeddings go to the same LLM host through `src/llm` (counted by the same spend cap and request limiter), model from `APP_EMBED_MODEL` (default `text-embedding-3-small`, which many free providers do not serve). The vector column is 1024 wide, so the model must return 1024 dims (the request asks for it). `preflight --online` tests this with one embedding call.

Do not switch embedders after memory rows exist: the index records the model and startup refuses a different one (exit 3, "migration required"). For a throwaway test DB: `docker compose down -v` and start again.

## 4. Character card (R9): what the placeholder means

`personality/character_card.md` is a placeholder and has no approval pin. Production startup **refuses** it (exit 2, "character card not usable"). For a local test only, bypass the gate:

```powershell
$env:NIMBUS_CARD_DEV_MODE = "1"      # must be in the process environment; .env is not read for it
```

plus `APP_ENV=development` (in `.env` above). Startup then logs a loud `CHARACTER CARD GATE BYPASSED` warning every time. Consequence: the assistant uses the placeholder text as its persona, so expect a bland assistant. With `APP_ENV=production` and the switch set the process refuses to start. Writing the real card and pinning its hash is a human step (ADR-0015); nothing here edits the card.

## 5. Start Postgres and migrate

```powershell
docker compose up -d                      # starts postgres only, published on 127.0.0.1:5432
uv sync --extra dev
uv run --extra dev alembic upgrade head   # reads POSTGRES_* from .env
```

If port 5432 is taken by another Postgres, stop that one or you will migrate the wrong database.

## 6. Preflight

```powershell
uv run --extra dev python -m src.app preflight             # offline: env, DB, migrations, card
uv run --extra dev python -m src.app preflight --online    # plus ONE Telegram getMe, ONE tiny model call (+1 embedding call with APP_EMBEDDER=api)
```

Exit 0 = no FAIL (WARN is allowed). Output lists `[PASS] / [WARN] / [FAIL]` per item with the exact fix command, and never prints a value. `--online` goes through `src/net` and `src/llm`, so the spend cap and audit row apply; it is skipped if the offline part has a FAIL.

## 7. Run

```powershell
$env:NIMBUS_CARD_DEV_MODE = "1"
uv run --extra dev python -m src.app 2>&1 | Tee-Object -FilePath "$env:TEMP
imbus.log"
```

Expected: `nimbus started env=development loops=3`. Logs go to the console (stderr); `Tee-Object` also saves them to `%TEMP%
imbus.log` (outside the repo, so it cannot be committed). More detail: `APP_LOG_LEVEL=DEBUG`. Secrets are scrubbed from logs (R12). Tail from a second window: `Get-Content "$env:TEMP
imbus.log" -Wait -Tail 50`. Health from a second window: `uv run --extra dev python -m src.app healthcheck` (exit 0 healthy).

Stop with Ctrl-C (clean exit within `APP_SHUTDOWN_GRACE_S`, default 10 s). Exit codes: 2 startup refused (config / card), 3 embedding migration required, 4 a loop failed fatally (for example a revoked bot token).

## 8. What to send first

1. Any message, e.g. `hi, who are you?`: expect a reply within seconds. Only you get replies.
2. Memory: send a message of 40+ characters stating a fact (`My dog is called Biscuit and I live in Pune, remember that.`). Memory extraction runs as a background job after the turn (needs a working embedder). Check: `docker compose exec postgres psql -U nimbus -d nimbus -c "select count(*) from memory_chunk"` (new rows, once the job ran). Later ask `what is my dog called?` to exercise recall. Chunks start untrusted and pass through the quarantine lane (extra model call per item, bounded by `APP_QUARANTINE_CALLS_PER_TURN`).
3. Spend and calls: `docker compose exec postgres psql -U nimbus -d nimbus -c "select purpose, status, cost_inr, created_at from llm_audit order by created_at desc limit 10"`.
4. **Reminders and tasks: off by default.** `reminders_create` and `tasks_create` are denied by policy (`PENDING_IDEMPOTENCY`, `src/policy/config.py`); the bot will tell you it cannot. To turn them on, set `POLICY_ENABLE_CREATES=true` (exactly `true`/`false`) in `.env` and restart; the boot log then says `policy creates enabled by operator`. Creation is idempotent per message; in a turn that read untrusted content it still needs an approval. Reading works either way. Other writes need approvals (`APPROVAL_HMAC_KEY`), and Google/Notion tools need their own credentials.
5. There are no slash commands (`/memory`, `/panic`) on this branch; do not look for them.

## 9. Smoke test with no credentials (fake backends)

Proves the process boots, loops and shuts down, with no Telegram, model or network. Still needs migrated Postgres:

```powershell
$env:APP_FAKE_BACKENDS = "true"; $env:APP_ENV = "development"; $env:NIMBUS_CARD_DEV_MODE = "1"
uv run --extra dev python -m src.app
```

Expected log: card BYPASSED warning, `approvals off`, `nimbus started env=development loops=3`. Fakes are refused unless `APP_ENV=development`. Clear the three variables afterwards (`Remove-Item Env:APP_FAKE_BACKENDS`) or the real run will use fakes.

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `invalid configuration: allowed_user_id` (field names only), exit 2 | a required setting is missing or malformed | `preflight` names the variable; token is `<digits>:<20+ chars>`, user id a positive integer |
| `startup refused: character card not usable`, exit 2 | placeholder card, no dev switch | step 4: `$env:NIMBUS_CARD_DEV_MODE="1"` and `APP_ENV=development` in the same shell |
| Same, switch set | `APP_ENV` is `production` (default) | put `APP_ENV=development` in `.env` |
| Bot silent, no errors | wrong `TELEGRAM_ALLOWED_USER_ID` (messages from others are dropped), or you never pressed Start | use the numeric id from `@userinfobot` |
| Bot silent, log shows conflict (409) | another process polls the same token (old run, webhook) | stop the other instance |
| Exit 4, Telegram unauthorized | token revoked / mistyped | new token from BotFather; `preflight --online` |
| LLM errors `status=429` / "busy" reply | free-tier quota | lower `LLM_RPM_LIMIT` / `LLM_DAILY_REQUEST_LIMIT` below the key's real quota, wait, use `APP_EXTRACT_MODE=cue` |
| `status=401/403` | key rejected | recheck `LLM_API_KEY`; `preflight --online` |
| `status=404` | wrong model id or base path | check `LLM_MODEL`; many providers want `/v1` (or their own path) in `LLM_BASE_URL` |
| `LLM egress would be denied` / base URL FAIL | http, localhost or port in `LLM_BASE_URL`; stray `NET_LLM_BASE_URL` | https URL on the default port |
| Refused: spend cap in the log | daily INR cap reached (price left at the 250/1000 placeholder) | set both `LLM_PRICE_*` for your model |
| Memory never saved, log has embedder errors | local embedder without package or weights; or provider lacks the embedding model | step 3; `preflight` WARN says which |
| Exit 3 "embedding model changed" | embedder switched after rows exist | `docker compose down -v` (test DB only) or switch back |
| `database migrations` FAIL or `relation ... does not exist` | not migrated | `uv run --extra dev alembic upgrade head` |
| `database: cannot connect` | Docker Desktop not running / container down / wrong `POSTGRES_*` | start Docker Desktop, `docker compose up -d`; a changed `POSTGRES_PASSWORD` needs `docker compose down -v` |
| `No module named 'asyncpg'` etc. | dev extra dropped by a bare `uv run` | `uv sync --extra dev` and use `uv run --extra dev` |
| Data key set in `.env` has no effect | raw environment read | set `$env:NIMBUS_DATA_KEY` in the shell (preflight says so) |
