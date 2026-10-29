# 0021 — Notion egress: no new host; `api.notion.com` narrowed to four calls

- Status: Proposed (P3.7). Needs human sign-off (HUMAN_TODO H26). Refines ADR-0004. R13 is **tightened, not widened**.

## Context
ADR-0004 already allow-lists `api.notion.com` (`Service.NOTION`, port 443 only) and `FIXED_HOSTS` already holds it, so P3.7 adds **no host and no `Service`**. Until now `Service.NOTION` could reach any path with any method (page updates, block deletion, archive, comments, user listing, database queries and anything Notion adds). Nimbus needs four calls only. This ADR is raised because the Notion tool is the first code that uses the host, and the choke point should not be broader than its only caller.

## Decision
`deny_notion_unexpected` (default rule, `src/net/policy.py`) plus an original-path check in `normalise_path` allow, for `Service.NOTION`, exactly:

| Method | Path | Use |
|---|---|---|
| POST | `/v1/search` | find shared pages |
| GET | `/v1/pages/<uuid>` | page title, archived flag |
| GET | `/v1/blocks/<uuid>/children` | read page text |
| PATCH | `/v1/blocks/<uuid>/children` | append blocks (the only write) |

`<uuid>` is a dashed lowercase UUID, matched against the ORIGINAL path (case-sensitive; trailing slash, extra segments, un-dashed ids, `/V1/...` are refused). Any other path denies with `notion_path`; a known path with another method denies with `notion_method` (for example `PATCH /v1/pages/<id>` which would archive a page, `DELETE /v1/blocks/<id>`, `PATCH /v1/blocks/<id>`). A Gmail send path keeps reason `gmail_send_path`. Tests: `tests/unit/net/test_r13_notion_paths.py` (all denials make zero transport calls).

Tool design (src/tools/integrations/notion.py): `notion_search` and `notion_read` are READ; `notion_append` is WRITE_EXTERNAL (approval card + TOTP step-up under the P3.1 policy). There is no update, delete, archive, share, comment or permission tool or client method.

Idempotency: Notion has no idempotency key. The appended text carries a small trailing marker `[nimbus:<key>]` where `key = sha256(source_event_id | page | text)[:20]`; before appending, the client scans the page's blocks (up to 10 requests of 100 blocks) for that marker and reports `already_appended` instead of writing again. A page too long to scan fails closed. The key contains the turn's stored event id, which a page author cannot know, so page content cannot pre-plant a marker to suppress a write.

## Consequences
- A compromised caller holding `Service.NOTION` can search, read shared pages and append blocks; it cannot delete, edit, archive, share or comment.
- The generic client tests that used `Service.NOTION` as a neutral target now use valid Notion paths (or `Service.TELEGRAM` where the method matters).
- Adding another Notion endpoint needs an edit to this table and a test (intended friction).

## Alternatives
- Leave the host open to all paths: rejected, unnecessary reach.
- Idempotency via a local table of done keys only: rejected, a restart between write and record would duplicate; the marker lives with the data.
