# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Ablation test for the hybrid retrieval scoring function (MEM-6):
docs/AGENT_OPERATING_MANUAL.md 3.3 requires "retrieval ablation run: every
scoring term shown to contribute". For each term, we hold every other term
fixed and show that changing it (or zeroing its weight) changes the score —
i.e. the term is not dead weight.
"""

from dataclasses import replace

import pytest

from src.memory.retriever import (
    RetrievalWeights,
    ScoringTerms,
    cosine_similarity,
    score,
    set_overlap,
)

BASELINE = ScoringTerms(
    cosine_sim=0.5,
    graph_proximity=0.5,
    age_days=10.0,
    importance=0.5,
    speaker_relevance=0.5,
    verified_bonus=0.5,
)


@pytest.mark.parametrize(
    "field",
    ["cosine_sim", "graph_proximity", "importance", "speaker_relevance", "verified_bonus"],
)
def test_term_contributes_when_increased(field: str) -> None:
    """Raising one term's value (weight held at default 1.0) raises the
    total score — i.e. the term is not ignored by `score()`.
    """
    baseline_score = score(BASELINE)
    raised = replace(BASELINE, **{field: 1.0})
    assert score(raised) > baseline_score


def test_recency_term_contributes() -> None:
    """A more recent memory (smaller age_days) scores higher than an older
    one, all else equal — recency is not dead weight.
    """
    recent = replace(BASELINE, age_days=0.0)
    old = replace(BASELINE, age_days=365.0)
    assert score(recent) > score(old)


@pytest.mark.parametrize(
    "weight_field",
    ["w_sim", "w_graph", "w_rec", "w_imp", "w_spk", "w_ver"],
)
def test_zeroing_a_weight_removes_that_terms_contribution(weight_field: str) -> None:
    """Ablation: zeroing a single weight must change the total score
    relative to the default weights, proving that weight/term pair was
    doing something. If it didn't change the score, the term would be
    provably dead weight (the failure MEM-6 guards against).
    """
    default_weights = RetrievalWeights()
    ablated_weights = replace(default_weights, **{weight_field: 0.0})
    assert score(BASELINE, ablated_weights) != score(BASELINE, default_weights)


def test_cosine_similarity_identical_vectors_is_one() -> None:
    vec = [1.0, 2.0, 3.0]
    assert cosine_similarity(vec, vec) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_empty_vector_is_zero() -> None:
    assert cosine_similarity([], []) == 0.0


def test_set_overlap_shared_id_is_one() -> None:
    assert set_overlap(frozenset({"a", "b"}), frozenset({"b", "c"})) == 1.0


def test_set_overlap_disjoint_is_zero() -> None:
    assert set_overlap(frozenset({"a"}), frozenset({"b"})) == 0.0


def test_set_overlap_empty_either_side_is_zero() -> None:
    assert set_overlap(frozenset(), frozenset({"a"})) == 0.0
    assert set_overlap(frozenset({"a"}), frozenset()) == 0.0


def test_age_days_is_measured_from_the_reference_and_floored() -> None:
    from datetime import UTC, datetime, timedelta

    from src.memory.retriever import age_days, recency_term

    ref = datetime(2026, 3, 1, tzinfo=UTC)
    assert age_days(ref - timedelta(days=10), ref) == pytest.approx(10.0)
    assert age_days(ref + timedelta(days=3), ref) == 0.0  # newer than reference: not boosted
    # an as_of query ranks as it would have then: same row, earlier reference => fresher
    then = ref - timedelta(days=30)
    assert recency_term(age_days(then, then + timedelta(days=1))) > recency_term(
        age_days(then, ref)
    )
