# LoCoMo baseline (pre-P1.3b): Qdrant store + DeterministicStubEmbedder

Measured by the P1.3b task before any code change, on `pivot` @ dbffde0
(unmodified `tests/eval/locomo/harness.py`, real Qdrant 1.11.4 + Postgres from
docker compose). Qwen3 / sentence-transformers was NOT loaded: an inline script
swapped `src.memory.manager.Qwen3Embedder` for `DeterministicStubEmbedder`
before calling `harness.run()` (all 10 samples, top_k=5, ~51 s wall).

Command (from the repo root):

    python -I <scratch>/baseline.py   # sys.path.insert(0, "."); m.Qwen3Embedder = m.DeterministicStubEmbedder; asyncio.run(harness.run())

| category | correct / total | rate |
|---|---|---|
| single_hop | 74 / 841 | 8.8% |
| multi_hop | 16 / 282 | 5.7% |
| open_domain | 2 / 96 | 2.1% |
| temporal | 7 / 321 | 2.2% |
| adversarial (not scored) | 0 / 446 | - |
| **overall (excl. adversarial)** | 99 / 1540 | **6.43%** |

The stub vector is sha256-derived and not semantic, so this measures noise, not
retrieval quality. It is a plumbing baseline for the store port only. Target
(0.85) is not met and is not gated at this stage.

## After P1.3b (pgvector `memory_chunk` store, same stub embedder)

Same dataset, top_k=5, scoring unchanged; harness ported (scratch Postgres DB
per sample instead of a Qdrant collection). Command: same inline script but
`harness.run(embedder=DeterministicStubEmbedder())`.

| category | before (Qdrant) | after (pgvector) |
|---|---|---|
| single_hop | 74 / 841 | 74 / 841 |
| multi_hop | 16 / 282 | 16 / 282 |
| open_domain | 2 / 96 | 2 / 96 |
| temporal | 7 / 321 | 7 / 321 |
| **overall** | **6.4286%** | **6.4286%** |

Exact parity (same cosine ranking; HNSW is not used on these small per-sample
tables' planner path, and the rerank code is unchanged). Wall time rose from
~51 s to ~8 min: per-chunk Event+MemoryChunk ORM inserts with flushes, plus
CREATE/DROP DATABASE and HNSW index creation per sample. Acceptable for an eval;
batching inserts is a follow-up.
