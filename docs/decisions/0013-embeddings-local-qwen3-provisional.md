# 0013 — Embeddings: local Qwen3-Embedding-0.6B (1024-d) by default, API fallback behind the same protocol

- Status: Provisional. Resolves the Brief §7 "Embeddings" default for design purposes. P2.3 confirms or flips it with a measurement on arm64.

## Context
The Brief's rule is local if the VM has ≥ 3 GB RAM headroom, else API. The A1 VM is 2 OCPU / 12 GB (verified 2026-10-24, see TARGET_DESIGN §0). The estimated budget is Postgres ~1 GB, app plus Python ~0.5 GB, OS ~1 GB, and the 0.6B model at fp32 in torch ~2.5–3 GB. That leaves well over 3 GB of headroom, but this is **an estimate, not a measurement**. CPU throughput on 2 Ampere cores is unmeasured. The previous x86 dev machine took ~13 min for ~600 single-chunk embeds without batching (MEMORY.md AD-21 tech debt).

## Decision
- Default to `Qwen3Embedder` (existing code, Apache-2.0), batched (≤ 32 texts), run in a worker thread with `torch.set_num_threads(2)`. The model weights are baked into the arm64 image at build time and loaded with `HF_HUB_OFFLINE=1`.
- Dimension 1024. `model_id` and `dim` are stored on every `memory_chunk`.
- An API fallback lives in `src/llm/embeddings_api.py`, so it is audited and spend-capped (R4).
- Flip criterion, measured in P2.3: embedding p95 > 1.5 s for a single query on the target class, or RSS headroom < 3 GB under soak. Either one switches the default to API via config and records an ADR update.
- `sentence-transformers` and torch become an optional extra so an API-only image stays small.

## Consequences
- Zero per-embed cost, and memory text never leaves the VM for embedding.
- Image size grows by roughly 1–2 GB (torch CPU arm64 plus weights). Unmeasured.
- Recorder transcripts embed in background jobs, so slow batches do not block replies.

## Alternatives
- **API embeddings from day one.** This is the fallback. It adds cost and sends all memory text to a second processor.
- **A smaller local model (e.g. a MiniLM-class 384-d model).** Kept as an option if the 0.6B fails the flip criterion but API is undesirable. It would need a LoCoMo comparison.
