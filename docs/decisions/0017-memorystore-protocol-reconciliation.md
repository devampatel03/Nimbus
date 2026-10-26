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
Nothing is updated in place. Valid time: `as_of`. Record time: `known_at`.

- **Chunks:** valid `event.occurred_at <= as_of`; visible at `known_at` iff `created_at <= known_at` and `invalidated_at` is NULL or `> known_at`. No `known_at` = not invalidated now.
- **Graph edges:** the valid-time instant is `as_of`, else `known_at`, else now (round 1, M3: the default read is `valid_from <= now < valid_to`, so a future-dated successor does not displace today's fact). `valid_from` is inclusive, `valid_to` exclusive. Record time: `ingested_at <= known_at` and not invalidated by `known_at`.
- **Derived valid_to (round 1, H2):** with `known_at`, an edge's `valid_to` counts only if its successor (self-join on `superseded_by`) was recorded by then: `CASE WHEN succ.id IS NULL OR succ.ingested_at <= known_at THEN valid_to ELSE NULL END`. A back-dated correction therefore does not retroactively close the old fact for earlier record times (Pune valid day 1, Mumbai valid_from day 3 recorded later: `known_at=day5` returns Pune). The derived value is also what `MemoryResult.valid_to` reports. Equal timestamps count as known (`<=`). Covered by the (as_of, known_at) timeline tests, including back-dated, future-dated and equal-timestamp rows.

### Sign-off: edit to `GraphRepository.supersede` (KEEP file `src/memory/graph.py`)
`supersede` now sets the old edge's `valid_to = max(new.valid_from, old.valid_from)` instead of `now()`. Reason: `as_of` reads need the old interval to end where the successor begins; `now()` made `as_of` between the successor's `valid_from` and the supersession time return both facts, or neither. Edge cases:

- Forward supersession (`new.valid_from >= old.valid_from`, the T2.9 case): old `valid_to = new.valid_from`; `object_*`, `valid_from` and `superseded_by` semantics are unchanged.
- Zero-length interval (`new.valid_from < old.valid_from`, a correction dated before the old fact began): old `valid_to == old.valid_from`, an empty interval `[vf, vf)` that is never valid at any instant (never negative, which `max` guarantees). The new fact is the only one valid from its own `valid_from`. Tested in `tests/integration/test_graph_timeline.py`.
- The orchestrator granted the architect-equivalent sign-off for this edit in the PR #23 round-1 review message; the INV-3 AST scan (`tests/eval/invariants/test_inv3_graph_facts_immutable.py`) still passes.

## Promotion (R8, round 1, H1)
`promote(item_id, approval_id)` is one atomic UPDATE requiring the approval to be: `status='consumed'`, `consumed_at <= expires_at` (consumed in time), `tool='memory'`, `action='promote'`, `payload->>'item_id' = item_id`; and the chunk to be unpromoted and not invalidated. Single use: partial UNIQUE indexes `uq_memory_chunk_promoted_approval` / `uq_graph_edge_promoted_approval` on `promoted_approval_id` (migration `c4e8a1d92b37`, one new head) make a second item with the same approval fail in the database. Whoever creates the approval (P3.2/P4.6 review flow) must write `{"item_id": "<uuid>"}` in the payload with tool `memory`, action `promote`. A `promoted_at` column is out of scope (follow-up if a timestamp is needed; `pending_action.consumed_at` is the proxy). `record_event` raises `EventConflictError` when a replayed `source_key` carries a different lane or kind, so an untrusted event cannot silently resolve to a trusted one.

## Consequences
- `MemoryManager.write_episodic(event_id, chunks)` replaces `write_episodic(session_id, EpisodicChunk[])`; `src/orchestrator/context.py:StoreRecall` and the LoCoMo harness were updated. `src/api/routes/memory.py` is unchanged and still importable.
- Callers must record the event first. Ingest/orchestrator wiring (P2.1b/P2.5b) passes the real `event_id`.
- `src/api/main.py` lifespan calls `MemoryManager.verify_index()` (interim wiring until the P2.1b entrypoint): a stored index from another model/dim stops startup with `EmbeddingMigrationRequired`; an unreachable database does not (see `/health`). The lazy per-request guard caches verified `(model_id, dim)` per process.
- Compile-forced or approved edits outside the P2.4 file list: `src/memory/graph.py` (supersede, above), `src/memory/schemas/{memory_chunk,graph}.py` (the two unique indexes, to match the migration), `src/memory/manager.py`, `src/orchestrator/context.py` (`StoreRecall` follows the new `search`), `src/api/main.py` (lifespan), `tests/integration/test_manager.py`, `tests/integration/test_pgvector_store.py`, `tests/_memory_helpers.py` (new helper), `tests/eval/locomo/harness.py` (protected, H14: event-linked ingest and the `BatchEmbedder` annotation).
