"""The AUC implementation and the evidence used to score it.

These numbers are published in the README, the UI and the MCP instructions, and
agents are told to trust the evidence tiers because of them. A wrong AUC is not
a wrong number, it is a wrong instruction.
"""
from __future__ import annotations

import numpy as np
import pytest

from git_synapse.analysis.validate import _auc


def _reference_auc(scores, labels):
    """Definitional AUC: the probability a random positive outranks a random
    negative, counting ties as half. Deliberately O(n^2) and obvious."""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


@pytest.mark.parametrize(
    ("scores", "labels", "expected"),
    [
        # Perfect separation, and its exact inverse.
        ([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], 1.0),
        ([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1], 0.0),
        # Every score identical: no information, so a coin flip.
        ([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1], 0.5),
        # One clean swap away from perfect.
        ([0.1, 0.9, 0.8, 0.2], [0, 0, 1, 1], 0.5),
        # Minimal case: one positive, one negative.
        ([0.0, 1.0], [0, 1], 1.0),
        ([1.0, 0.0], [0, 1], 0.0),
    ],
)
def test_auc_against_known_values(scores, labels, expected):
    got = _auc(np.array(scores, float), np.array(labels))
    assert got == pytest.approx(expected)


@pytest.mark.parametrize("labels", [[0, 0, 0], [1, 1, 1]])
def test_auc_is_undefined_with_only_one_class(labels):
    """With no negatives there is nothing to rank against; nan, not 0 or 1.

    Returning a number here would let a degenerate label set masquerade as a
    perfect score.
    """
    got = _auc(np.array([0.1, 0.5, 0.9]), np.array(labels))
    assert np.isnan(got)


def test_auc_matches_the_definition_on_random_data():
    """Ties get average ranks; the rank-sum shortcut must agree with the
    pairwise definition, including when scores collide."""
    rng = np.random.default_rng(20260826)
    for _ in range(200):
        n = int(rng.integers(2, 40))
        # Coarse quantisation forces frequent ties, which is where a rank-sum
        # implementation usually goes wrong.
        scores = np.round(rng.random(n), 1)
        labels = rng.integers(0, 2, n)
        if labels.sum() in (0, n):
            continue
        assert _auc(scores, labels) == pytest.approx(_reference_auc(scores, labels))


def test_auc_is_invariant_to_monotone_rescaling():
    """AUC depends on order alone, so any increasing transform must not move it."""
    rng = np.random.default_rng(7)
    scores = rng.random(50)
    labels = rng.integers(0, 2, 50)
    base = _auc(scores, labels)
    for transform in (lambda x: x * 1000, lambda x: x + 5, np.exp, lambda x: x**3):
        assert _auc(transform(scores), labels) == pytest.approx(base)


def test_auc_handles_negative_and_extreme_scores():
    scores = np.array([-1e9, -1.0, 0.0, 1.0, 1e9])
    labels = np.array([0, 0, 1, 1, 1])
    assert _auc(scores, labels) == pytest.approx(1.0)


def test_reversing_labels_reflects_the_auc_about_half():
    rng = np.random.default_rng(11)
    scores = rng.random(60)
    labels = rng.integers(0, 2, 60)
    assert _auc(scores, labels) + _auc(scores, 1 - labels) == pytest.approx(1.0)


# ------------------------------------------------- evaluation over real data

def test_ground_truth_edges_respect_the_bump_threshold(db):
    """The label is "bumped more than min_bumps times"; a looser threshold can
    only ever admit more edges."""
    from git_synapse.analysis.validate import ground_truth_edges

    loose = ground_truth_edges(min_bumps=2)
    strict = ground_truth_edges(min_bumps=5)
    assert strict <= loose, "a stricter threshold cannot add edges"


def test_ground_truth_edges_are_directed_pairs(db):
    from git_synapse.analysis.validate import ground_truth_edges

    for edge in list(ground_truth_edges(min_bumps=2))[:20]:
        assert isinstance(edge, tuple) and len(edge) == 2
        assert edge[0] != edge[1], "a repository cannot bump itself"


def test_evaluate_returns_a_score_per_measure(db):
    from git_synapse.analysis.validate import evaluate
    from git_synapse.stats.registry import MEASURES

    scores = evaluate(lag_bins=1)
    scores = scores if isinstance(scores, list) else scores.measures
    if not scores:
        pytest.skip("no lag data")
    keys = {s.measure for s in scores}
    assert keys <= {m.key for m in MEASURES}
    for s in scores:
        assert 0.0 <= s.directional_accuracy <= 1.0
        assert np.isnan(s.auc) or 0.0 <= s.auc <= 1.0


def test_a_symmetric_measure_is_exactly_half_directional_at_lag_zero(db):
    """At lag 0 the joint matrix is M @ M.T, so a symmetric measure is identical
    in both orientations. Anything other than 0.5 would mean the directional
    accuracy is measuring something else."""
    from git_synapse.analysis.validate import evaluate

    scores = evaluate(lag_bins=0)
    scores = scores if isinstance(scores, list) else scores.measures
    if not scores:
        pytest.skip("no lag data")
    russell = next((s for s in scores if s.measure == "russell_rao"), None)
    if russell is None:
        pytest.skip("russell_rao not evaluated")
    assert russell.directional_accuracy == pytest.approx(0.5, abs=1e-9)


def test_sweep_lags_covers_the_configured_lags(db):
    from git_synapse.analysis.validate import sweep_lags

    out = sweep_lags(measure="npmi")
    if not out:
        pytest.skip("no lag data")
    # MeasureScore objects, one per lag that has rows.
    lags = [row.lag_bins for row in out]
    assert len(set(lags)) == len(lags), "one row per lag"
    assert all(row.measure == "npmi" for row in out)
    assert lags == sorted(lags), "lags must come back in order"


def test_compare_to_symmetric_reports_both_sides(db):
    from git_synapse.analysis.validate import compare_to_symmetric

    out = compare_to_symmetric()
    if not out or out.get("comparable_edges", 0) == 0:
        pytest.skip("nothing comparable")
    assert 0.0 <= out["symmetric_directional_accuracy"] <= 1.0
