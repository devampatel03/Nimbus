# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Hybrid retrieval scoring (docs/TECHNICAL_ARCHITECTURE.md section 6.3, MEM-6).

    score = w_sim   * cosine(query, memory)
          + w_graph * graph_proximity(memory, active_entities)
          + w_rec   * exp(-lambda * age_days)
          + w_imp   * importance
          + w_spk   * speaker_relevance(memory, present_speakers)
          + w_ver   * user_verified_bonus

Weights here are placeholders, not tuned values: MEM-6/MEM-7 require tuning
against the eval harness (LoCoMo/LongMemEval), which does not exist yet.
`ScoringTerms` isolates each pre-computed term so the ablation test
(tests/unit/test_retriever.py) can show every term contributes, per the
P-MEMORY checklist item "retrieval ablation run: every scoring term shown
to contribute" (AGENT_OPERATING_MANUAL.md 3.3).
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalWeights:
    w_sim: float = 1.0
    w_graph: float = 1.0
    w_rec: float = 1.0
    w_imp: float = 1.0
    w_spk: float = 1.0
    w_ver: float = 1.0
    recency_lambda: float = 0.05  # decay per day


DEFAULT_WEIGHTS = RetrievalWeights()


@dataclass(frozen=True)
class ScoringTerms:
    """Pre-computed inputs to the hybrid score for one candidate memory.

    Computing these terms (cosine similarity, graph-entity overlap, speaker
    overlap) from raw candidates is the caller's job (`MemoryManager`); this
    module only combines already-computed terms, which is what makes each
    term independently ablatable in tests.
    """

    cosine_sim: float  # 0..1, similarity(query, memory)
    graph_proximity: float  # 0..1, overlap with active_entities
    age_days: float  # >=0, time since the memory became true/known
    importance: float  # 0..1
    speaker_relevance: float  # 0..1, overlap with present_speakers
    verified_bonus: float  # 0..1, typically 0 or 1


def recency_term(age_days: float, weights: RetrievalWeights = DEFAULT_WEIGHTS) -> float:
    return math.exp(-weights.recency_lambda * age_days)


def score(terms: ScoringTerms, weights: RetrievalWeights = DEFAULT_WEIGHTS) -> float:
    return (
        weights.w_sim * terms.cosine_sim
        + weights.w_graph * terms.graph_proximity
        + weights.w_rec * recency_term(terms.age_days, weights)
        + weights.w_imp * terms.importance
        + weights.w_spk * terms.speaker_relevance
        + weights.w_ver * terms.verified_bonus
    )


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def set_overlap(memory_ids: frozenset[str], active_ids: frozenset[str]) -> float:
    """1.0 if `memory` shares at least one id with `active`, else 0.0.

    Used for both `graph_proximity` (active_entities) and
    `speaker_relevance` (present_speakers) — both are "does this memory
    touch something in play right now" overlap checks.
    """
    if not memory_ids or not active_ids:
        return 0.0
    return 1.0 if memory_ids & active_ids else 0.0
