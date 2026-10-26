# 0018 — LoCoMo retrieval gate: provisional 0.70 / target 0.85 stays; real-embedder baseline NOT MEASURED

- Status: Provisional (P2.4). Does **not** change `quality/thresholds.yaml` (`locomo_min_accuracy_provisional: 0.70`, `locomo_target_accuracy: 0.85`). Calibration happens only via a later amendment to this ADR with a measured number.

## Context
The harness (`tests/eval/locomo/harness.py`) scores token-overlap recall@k over the public LoCoMo-10 set, per category, excluding adversarial. Both thresholds were set before any retrieval was measured with a semantic embedder. The only numbers that exist were produced with `DeterministicStubEmbedder` (sha256-derived vectors, not semantic), which measures plumbing, not retrieval quality.

## What was measured (P2.4, stub embedder, 2 of 10 samples, top_k=5)
| | origin/pivot b17bb8c | P2.4 |
|---|---|---|
| correct / scored | 13 / 233 (5.58%) | 13 / 233 (5.58%) |
| per category | single_hop 9/114, multi_hop 4/43, temporal 0/63, open_domain 0/13 | identical |
| wall time | 64.6 s | 22.0 s (one embed batch per LoCoMo session instead of per turn) |

This proves the retrieval pipeline change (event-linked chunks, rerank, filters) is neutral on identical vectors. It says nothing about the gate: 5.6% is noise-level and is **not** a baseline for 0.70.

## What was NOT measured
The real-embedder score (Qwen3-Embedding-0.6B or the API embedder). The dev machine must not load sentence-transformers (memory-crash risk), and no API key exists (H3). **Any statement of a real LoCoMo number before the run below is invalid.**

## Decision
1. Keep 0.70 as a provisional floor and 0.85 as the target. Neither is lowered, raised or waived here.
2. The gate is evaluated only by a real-embedder run. Until one exists, the LoCoMo gate is reported as NOT MEASURED, never as pass or fail.
3. Measurement procedure (VM per H6, or an API-embedding run once H3 provides a key):
   - `uv sync --extra local-embed`; weights prefetched into the image; `HF_HUB_OFFLINE=1`.
   - `uv run python tests/eval/locomo/harness.py --top-k 5` (all 10 samples), 3 runs; record per-category correct/total, overall, wall time, peak RSS (`/usr/bin/time -v`) and the embed p50/p95.
   - Run the same with `top_k` 5 and 10 and with rerank weights at the defaults (all 1.0) versus similarity-only (`RetrievalWeights(w_graph=0, w_rec=0, w_imp=0, w_spk=0, w_ver=0)`) so the rerank's contribution is shown, not assumed.
   - Record results in `docs/pivot/reports/` and amend this ADR.
4. Calibration rule: if the measured score is below 0.70, the first response is to fix retrieval (query instruction prefix, chunking by turn windows, `top_k`, weights), not to lower the gate. Lowering requires a new ADR stating the measured number, the reason, and reviewer sign-off.
5. Known factors expected to move the number, all untested: Qwen3 query-instruction prefix (not applied; see ADR-0017), the token-overlap scorer (a lexical proxy, not LLM-judged answer accuracy), and category 2 (temporal) and 3 (open domain), which need reasoning that single-shot retrieval cannot supply.

## Consequences
CI keeps the harness runnable (plumbing) but does not gate on a score. P2.4's acceptance ("re-baseline recorded") is met for the stub; the real-embedder row stays open on H6/H3.
