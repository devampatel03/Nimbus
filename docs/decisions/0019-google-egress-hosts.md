# 0019 — Google egress: no new hosts; `oauth2.googleapis.com` narrowed to `POST /token` and `POST /revoke`

- Status: Accepted (P3.4). Refines ADR-0004 and TARGET_DESIGN §3. R13 is **tightened, not widened**.

## Context
P3.4 needs to exchange an authorisation code, refresh access tokens and revoke tokens. ADR-0004 already allow-lists `oauth2.googleapis.com` (`Service.GOOGLE_OAUTH`), `www.googleapis.com` (`Service.GOOGLE_APIS`) and `gmail.googleapis.com` (`Service.GMAIL`), port 443 only. So **no new host is needed** and the R13 allow-list does not grow.

The existing rules only restricted *which paths are denied* (Gmail send/import/insert, Gmail settings writes, `/batch`). `Service.GOOGLE_OAUTH` could still reach any path on its host (`/tokeninfo`, `/oauth2/v3/userinfo`, device-code endpoints, and anything Google adds) with any method. Nothing in Nimbus calls those.

## Decision
- No host, port or `Service` is added. `accounts.google.com` (the consent page) is **not** allow-listed: only the operator's browser opens it; no Nimbus process connects to it.
- New policy in `src/net/policy.py`. For `Service.GOOGLE_OAUTH` it allows exactly:

  | Method | Path | Use |
  |---|---|---|
  | POST | `/token` | authorization-code exchange (PKCE) and refresh |
  | POST | `/revoke` | revoke a refresh or access token |

  The path must match the **original, unmodified** string exactly: `/TOKEN`, `/%74oken` and `/token/` are denied (checked in `normalise_path`, before the lowercased/decoded form is produced for the other rules). Any other path denies with reason `google_oauth_path` (Gmail send paths keep reason `gmail_send_path`); any other method on those paths denies with `google_oauth_method` (rule `deny_google_oauth_unexpected`, applied in `EgressClient.request`, on same-host redirect hops and in `_GuardTransport`).
- `deny_gmail_send`, `deny_gmail_settings_write` and `deny_google_batch` are unchanged and keep applying on **every** Google host (`Service.GMAIL`, `GOOGLE_APIS`, `GOOGLE_OAUTH`). `tests/unit/net/test_r13_google_oauth_paths.py` asserts send, drafts-send, import and insert (including `:send`, `/upload/...` and percent-encoded forms) are denied on all three with zero transport calls.
- `www.googleapis.com` / `gmail.googleapis.com` path patterns for Calendar, Gmail drafts and Drive are owned by P3.5, P3.6 and P3.8; this ADR does not change them.

## Consequences
- A compromised or buggy caller holding `Service.GOOGLE_OAUTH` can mint or revoke tokens only; it cannot probe the host's other endpoints.
- Existing generic tests that issued `GET /ok` to every service now use `POST /token` for `GOOGLE_OAUTH`.
- Adding another OAuth endpoint later (for example `tokeninfo`) needs an edit to this table and a test, which is the intended friction.

## Alternatives
- **Leave the host open to all paths.** Rejected: unnecessary reach, and Google fronts several APIs behind shared front ends.
- **Allow-list `accounts.google.com` so the script can fetch discovery documents.** Rejected: not needed, and the consent URL is constructed locally.

## Addendum (P3.8, Drive read-only)
Drive uses `www.googleapis.com` (`Service.GOOGLE_APIS`) at `/drive/v3/...`; no host or `Service` is added. R13 is tightened: default deny rule `deny_drive_write` refuses every method except GET/HEAD on paths matching `^(/resumable)?(/upload)?/drive/v<N>[a-z0-9]*` (reason `drive_write`), on all services, after path decoding and lowercasing. This covers v2/v3, the `/upload` and `/resumable/upload` forms and version suffixes such as `v3beta`. It is a path-shape rule, not proof that no other Google path reaches Drive: the real guarantee is that `DriveReader` has no write method and the token only holds `drive.readonly`. Search design (M-3): one `UntrustedText` per file, at most 5 files per search (`MAX_LIST_RESULTS` = loop `MAX_EXTERNAL_ITEMS`), so a search costs up to 5 quarantine calls and a read costs 1; the worst case per turn is bounded by the turn tool-call cap times 5. One item per file gives each name its own DataBlock in file order, instead of one positional free-text list the quarantine model could summarise or a name could forge. Names and owners are reduced to one line (control, format and separator characters dropped). Scope is `drive.readonly` (Q4/H17), already in the P3.4 base set.
