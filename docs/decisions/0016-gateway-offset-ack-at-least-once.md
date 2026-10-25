# 0016 — Gateway: offset advances on ack, at-least-once delivery, bounded redelivery

- Status: Accepted (P2.1a round 1). Refines TARGET_DESIGN §4.1 step 1 ("INSERT inbound_update, then store the offset").

## Context
R1 requires that a restart neither loses nor duplicates inbound messages. Telegram deletes an update once `getUpdates` is called with a higher `offset`, and keeps unconfirmed updates for about 24 hours. `inbound_update` (P1.2c) stores `update_id`, sender, kind and timestamps, but no message body.

If the offset advanced when an update was merely received, a crash before the turn was stored would lose the message, because the body is gone from both Telegram and Postgres.

## Decision
- The persisted offset (`system_flag` key `telegram.offset`) moves past an update only in the same transaction that marks it processed (`Gateway.ack`). The value is `min(unprocessed update_id)`, else `max(update_id) + 1`, and never decreases.
- Delivery is **at least once**. A crash after the consumer stored its event but before `ack` redelivers the update. The consumer must be idempotent on `event.source_key = "telegram:<update_id>"` (acceptance item for P2.2 ingest). The gateway alone does not guarantee "no duplicate effect".
- Redelivery is bounded. Each redelivery of an unacked update increments a persisted counter (`system_flag` key `telegram.redeliveries`, so a crash loop still counts). Past `max_deliveries` (default 5) the update is marked processed with no event, logged by `update_id` only, and the offset moves on. One poison update therefore cannot pin the offset or starve the queue behind it. The `kind` column is not rewritable (append-only trigger), so poison rows are identified by `processed_at` set with `event_id` NULL plus the log line.
- A poll that returns only already-delivered, unacked updates backs off exponentially (1 s to 30 s) instead of re-polling immediately.
- Updates from other users, unsupported kinds and malformed updates are recorded as already-processed audit rows (id, sender id, kind; no content), which advances the offset.

## Consequences
- No lost messages across restarts and crashes within Telegram's retention window. Downtime longer than about 24 hours loses updates under any design, because Telegram discards them.
- A consumer that fails to ack repeatedly sees the same update up to the budget, then loses it (logged). This favours liveness over blocking the single user's queue.
- Gateway tests must cover: replay after restart, out-of-order acks, atomic offset and processed flag, poison budget across restarts, more than 100 queued updates behind an unacked one.

## Alternatives
- **Add a payload column to `inbound_update` and advance the offset on receipt.** Rejected: it stores plaintext message bodies in a second table, adds a retention and purge burden (R-privacy), and needs a migration. The processed-offset approach needs neither.
- **Advance the offset on receipt and rely on the consumer.** Rejected: loses messages on a crash between receipt and event storage.
- **Unbounded redelivery.** Rejected: a single poison update stalls the whole queue (found in round 1 review).
