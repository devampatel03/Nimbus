# Runbook: Google OAuth (Calendar, Gmail drafts, Drive read-only)

Owner: operator (human steps H4, H16, H17). Code: `src/integrations/google/auth.py`, `scripts/google_oauth.py`. Decisions: ADR-0005, ADR-0007 (Proposed, H16 pending), ADR-0004, ADR-0019. Brief rules: R3, R6, R12, R13, R15.

## What this sets up

A single **refresh token**, obtained once on your own machine through a loopback consent flow (PKCE, `state` check, `127.0.0.1` on an ephemeral port, one request, then the listener closes). It is stored **encrypted** (AES-256-GCM, `NIMBUS_DATA_KEY`) in the Postgres `system_flag` table under key `google_oauth_credentials`. The bot turns it into short-lived access tokens held in memory only. The bot never serves the callback: the app opens no inbound port (R3).

Allowed scopes (exact; anything else is rejected at consent, at code exchange, at every refresh and when read back from the store):

| Scope | Default | Why |
|---|---|---|
| `https://www.googleapis.com/auth/calendar.events` | allowed | create and edit events (not whole-calendar admin) |
| `https://www.googleapis.com/auth/gmail.readonly` | allowed | search and read mail (output is untrusted data) |
| `https://www.googleapis.com/auth/drive.readonly` | allowed | read-only Drive (H17) |
| `https://www.googleapis.com/auth/gmail.compose` | **OFF (opt-in)** | create drafts; see below |

**`gmail.compose` is not part of the default and is not decided by this code.** Whether Nimbus may ever hold a draft-capable Gmail scope is the pending human decision **H16** (ADR-0007, status Proposed; its disclosure text in SECURITY.md and `/privacy` is not written yet). Until the human answers, leave it off: the script refuses `gmail-compose`, and the bot refuses any grant or stored token that contains it. To opt in *after* H16 is answered in favour, set `GOOGLE_ALLOW_GMAIL_COMPOSE=true` on both the operator machine and the VM, then re-run `authorize --scopes calendar,gmail-read,gmail-compose,drive-read`. Turning the flag off again makes the bot refuse the stored token (`scope_violation`) until you re-authorise without it.

Why it needs a human decision: Gmail has no drafts-only scope. A token holding `gmail.compose` can technically also send. Nimbus has no send method and `src/net` denies the send/import/insert paths, but the token itself could send if it were stolen from the VM. The strictest alternative (ADR-0007) is to stay on `gmail.readonly` only. `gmail.send`, `gmail.modify`, `mail.google.com`, full `drive` and full `calendar` are never requested and never accepted, whatever the flag.

## One-time Google Cloud setup (H4)

1. Create (or reuse) a Google Cloud project. Enable the **Google Calendar API**, **Gmail API** and **Google Drive API**.
2. **OAuth consent screen**: user type External; add the three default scopes above (add `gmail.compose` only if H16 is decided in favour); add your own Google account as a test user while setting up.
3. **Publish the app: set Publishing status to "In production" (R15).** This is mandatory.
   - If the app stays in **Testing**, Google expires the refresh token after **7 days**. Calendar, Gmail and Drive then fail with `invalid_grant` one week after every authorisation. The failure is silent until a tool call needs Google.
   - "In production" without Google verification is fine for a personal bot: tokens last until you revoke them or they go unused for about 6 months, and the app is capped at 100 users. The cost is the **unverified-app warning** below.
   - Verify the consent screen page reads "In production" after you publish, and again if the refresh token ever dies with `invalid_grant`.
4. **Credentials**: create an OAuth client ID of type **Desktop app**. Note the client ID and client secret. (For a desktop client the secret is not confidential in the Google sense, but treat it as one: it is `SecretStr`, never logged or committed.)
5. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the environment of the machine that runs the script (and the VM, for refreshes). Do not commit them.

## Authorise (run on your machine, not on the bot)

Prerequisites on the machine: repo checkout, `uv sync --extra dev`, the env vars above, `NIMBUS_DATA_KEY` (the same key the VM uses) and the `POSTGRES_*` settings pointing at the **bot's** database. Open a tunnel first, for example `ssh -N -L 5432:127.0.0.1:5432 <vm>` and use `POSTGRES_HOST=localhost`. Never expose Postgres publicly.

```
uv run python scripts/google_oauth.py authorize          # default: calendar, gmail-read, drive-read
```

1. The script prints a consent URL and opens it in your browser.
2. Pick your Google account. Because the app is unverified you will see **"Google hasn't verified this app"**. Click **Advanced**, then **Go to <app name> (unsafe)**. This is expected and is the one manual click-through (R15).
3. Keep **all boxes ticked** on the granular-consent screen (unticking one is allowed but later actions that need it fail with `scope_missing`).
4. The browser lands on `http://127.0.0.1:<port>/oauth2callback` and shows "Authorisation received". The listener has already closed. The script prints `OK` and the granted scopes.

Running the script on the VM instead: use `--no-browser --port 8765`, forward the port from your laptop (`ssh -L 8765:127.0.0.1:8765 <vm>`), and open the printed URL locally. The listener still binds `127.0.0.1` only.

Check, without exposing anything:

```
uv run python scripts/google_oauth.py status
NIMBUS_LIVE_GOOGLE=1 uv run python scripts/google_oauth.py smoke   # one live token refresh
```

`smoke` prints scopes only, never tokens. CI never contacts Google; all automated tests use recorded, scrubbed fixtures and a fake token endpoint.

## When something goes wrong

The bot raises fixed error codes (no secret text). Re-auth cases also send you a Telegram message once per 24 h (the app wires the notifier; the text is fixed).

| Code | Meaning | Do |
|---|---|---|
| `invalid_grant` | refresh token expired or revoked: Testing-mode 7-day expiry, you removed access at myaccount.google.com, password change on some accounts, or ~6 months unused | Check publishing status is "In production" (R15), then re-run `authorize`. The bot stops calling Google for that token until a new one is stored. |
| `no_token` | nothing stored yet | run `authorize` |
| `scope_violation` | the stored or returned grant has a scope outside the effective allowed set (including `gmail.compose` while the flag is off), or a refresh tried to add a scope the token never had; the token is never used | `revoke`, then `authorize` again; investigate how an extra scope got granted |
| `scope_missing` | grant lacks a scope the action needs (unticked box) | `authorize` again, keep all boxes ticked |
| `token_decrypt_failed` | `NIMBUS_DATA_KEY` differs from the key that sealed the token | restore the correct key, or `authorize` again to overwrite |
| `token_store_corrupt` | the `system_flag` row is malformed | `revoke` (it forgets an unreadable row), then `authorize` |
| `invalid_client` / `unauthorized_client` | client ID or secret wrong, or the client was deleted | fix `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` |
| `client_not_configured` | client ID or secret missing in env | set them |
| `network_error`, `server_error` | transient; retried with capped exponential backoff, at most 2 retries per call, then fails | none, retry later |
| `egress_denied` | `src/net` refused the request (service disabled in `NET_ENABLED_SERVICES`) | re-enable `google_oauth` |
| `callback_timeout`, `state_mismatch`, `authorization_denied`, `bad_callback` | consent flow problems on the script side | re-run `authorize`; `state_mismatch` means the redirect did not come from this run, do not retry the same URL |

## Revoke

```
uv run python scripts/google_oauth.py revoke
```

It revokes the token at Google (`POST /revoke`) and deletes the stored record. The local record is deleted **even if Google could not be reached**; the script then prints `revoke_failed`. In that case also remove the app at <https://myaccount.google.com/permissions>. Do this at once if the VM, a backup or `NIMBUS_DATA_KEY` may have leaked: revoking invalidates the refresh token even if an attacker copied the ciphertext and the key.

## Key rotation (`NIMBUS_DATA_KEY`)

The Google record is sealed with the single column key, which also seals other columns (for example `llm_audit` payloads, purged after 14 days). The Google token is cheap to re-issue, so rotate like this:

1. With the **old** key still set: `scripts/google_oauth.py revoke` (needs the old key to read the record; if it cannot, `revoke` still deletes it and you remove the app at myaccount.google.com/permissions).
2. Generate the new key (`python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`), update `NIMBUS_DATA_KEY` on the VM and on your machine, restart the bot.
3. Run `authorize` with the new key.

Rotating the client secret: create a new secret in the console, update `GOOGLE_CLIENT_SECRET`, restart. The refresh token stays valid (it is bound to the client ID, not the secret).

## Operational limits worth knowing

- The re-auth Telegram message is deduplicated in memory (once per 24 h per cause). After a bot restart a dead token is re-tried once and you may be told again; persisting that state is a follow-up.
- Access-token refresh is serialised by one lock per process: a slow Google outage can delay all Google tool calls for up to about 45 s each (timeout x retries) before they fail with `network_error` or `server_error`.
- Logs carry one fixed line for: successful authorisation (scope names only), revoke, a token turning dead (`invalid_grant`) and `scope_violation`. They never contain tokens or codes.

## Security properties to keep in mind

- Telegram, Oracle and the model provider can see plaintext they are sent; this runbook does not change that (Brief §8). The Google token never goes to the model provider: scrubbing blocks token-shaped strings in prompts and logs.
- Tokens, codes, the PKCE verifier and the client secret are `SecretStr`, are never logged, and never appear in exceptions or notifications (INV-7). Encryption at rest protects a database dump, not a compromised running process (it holds the key and the access token).
- The consent URL is not a secret; the authorisation code and verifier are, and live only in memory for the seconds of the flow.
- Access tokens are cached in memory only (about an hour) and are refreshed through `src/net` (`POST oauth2.googleapis.com/token` only, ADR-0019).

## Dependency and env summary

No new Python dependencies. New env vars: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOW_GMAIL_COMPOSE` (default false; H16) (optional tuning: `GOOGLE_MAX_RETRIES`, `GOOGLE_BACKOFF_BASE_S`, `GOOGLE_BACKOFF_MAX_S`, `GOOGLE_REQUEST_TIMEOUT_S`). Script-only: `NIMBUS_LIVE_GOOGLE=1` gates `smoke`. Needs the existing `NIMBUS_DATA_KEY`. No migration: the record lives in `system_flag`.
