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
