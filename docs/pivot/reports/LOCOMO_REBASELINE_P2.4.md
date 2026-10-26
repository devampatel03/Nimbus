# LoCoMo re-baseline (P2.4): stub embedder only; real embedder NOT MEASURED

Host constraint: Qwen3 / sentence-transformers was not loaded. `harness.run(max_samples=2, top_k=5, embedder=DeterministicStubEmbedder())`, real Postgres+pgvector, scratch DB per sample.

| | origin/pivot b17bb8c | task/P2.4-retrieval |
|---|---|---|
| single_hop | 9/114 | 9/114 |
| multi_hop | 4/43 | 4/43 |
| temporal | 0/63 | 0/63 |
| open_domain | 0/13 | 0/13 |
| overall (excl. adversarial 0/71) | 13/233 = 5.58% | 13/233 = 5.58% |
| wall | 64.6 s | 22.0 s |

Parity on identical (non-semantic) vectors: the event-linked write path, filters and rerank do not change ranking. Not a quality baseline. Gate status (provisional 0.70 / target 0.85): NOT MEASURED. Procedure and rules: docs/decisions/0018-locomo-provisional-gate.md.
