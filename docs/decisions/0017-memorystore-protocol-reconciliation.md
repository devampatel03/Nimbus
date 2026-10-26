# 0017 — MemoryStore protocol: reconcile the shipped P1.3b shape with TARGET_DESIGN §3

- Status: Accepted (P2.4). Documents deliberate deviations from the `MemoryStore` Protocol in TARGET_DESIGN §3 (`src/memory/protocols.py`).

## Context
P1.3b shipped `write_chunks(session_id, items) / search(query_vec, limit, session_id) / invalidate(id)`. It created a synthetic `event` row per chunk (review MED-1), exposed no lane on hits (MED-2), and took pre-computed vectors. TARGET_DESIGN §3 specifies `record_event / add_chunks(event_id, chunks) / assert_fact / retract / search(MemoryQuery) / facts_as_of / review_queue / promote`.

## Decision
`src/memory/store.py` now matches the design for the episodic path:

| TARGET_DESIGN | Shipped (P2.4) |
|---|---|
| `record_event(event)` idempotent on `source_key` | same (`telegram:<update_id>`; takes the `src.contracts.event.Event`) |
| `add_chunks(event_id, chunks)` embeds via injected Embedder, lane inherited | same; idempotent on `(event_id, chunk_index)`; replays are not re-embedded; `ChunkIn` has no `lane` field, confidence can only be lowered |
| `search(MemoryQuery) -> list[MemoryHit]` | same (`MemoryHit` is an alias of `ChunkHit`); `as_of` (valid time) and `known_at` (record time) added to `MemoryQuery`; hits carry `lane`, `untrusted`, `promoted` |
| `promote(item_id, approval_id)` | same; additionally requires `pending_action.status='consumed'` (the DB CHECK only requires a non-null id) |
| `retract` | `invalidate(chunk_id, superseded_by=None)`: stamps `invalidated_at`, raises `ChunkNotFoundError` on an unknown id, no-op if already invalidated (set-once column) |

Deliberate deviations:

1. **Embedder shape.** The store depends on `model_id`, `dim` and `async aembed_batch(texts)` (satisfied by `Qwen3Embedder` and `ApiEmbedder`). The design's `embed(texts, kind="query"|"document")` is not yet implemented by `src/memory/embedder.py` (P2.3, merged). Qwen3-Embedding's query instruction prefix therefore is **not applied**; this is expected to matter for the real baseline (see ADR-0018) and is a follow-up for the embedder owner.
2. **Not in the episodic store:** `assert_fact`, `facts_as_of`, `review_queue`. Facts stay in `GraphRepository` (`graph.py`, KEEP) behind `MemoryManager.write_semantic` / `query(as_of=, known_at=)`; the review queue is P4.6 (`src/memory/review.py`). Splitting them avoids a second write path for facts.
3. **`speaker_id`, `session_id`** have no columns on `memory_chunk`. Hits take them from `event.meta` (`speaker_id`, `session_id`) when the caller recorded them; otherwise `session_id` is the event id (the event is the new session, ADR-0014). The old per-search session filter is dropped: it was unindexed (`meta->>'session_id'`), unused outside tests, and single-user deployment makes it moot. A real per-conversation filter needs an indexed column (migration owned by the schema task).
4. `Embedder` (sync `embed(text)`) in `manager.py` is kept for legacy callers only.

## Bi-temporal semantics (INV-3)
Nothing is updated in place. Valid time: `as_of` (chunks: `event.occurred_at <= as_of`; edges: `valid_from <= as_of < valid_to`). Record time: `known_at` (`created_at <= known_at` and `invalidated_at` NULL or later). `GraphRepository.supersede` now closes the old edge's `valid_to` at the successor's `valid_from` (was `now()`), which `as_of` reads depend on. For edges, a query with only `known_at` uses it as the valid-time instant too.

## Consequences
- `MemoryManager.write_episodic(event_id, chunks)` replaces `write_episodic(session_id, EpisodicChunk[])`; `src/orchestrator/context.py:StoreRecall` and the LoCoMo harness were updated. `src/api/routes/memory.py` is unchanged and still importable.
- Callers must record the event first. Ingest/orchestrator wiring (P2.1b/P2.5b) passes the real `event_id`.
