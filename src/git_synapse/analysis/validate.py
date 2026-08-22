"""Validation: do the statistics actually recover real dependency propagation?

Every other module here *produces* numbers. This one asks whether they are any
good, using the manifest-bump edges from :mod:`git_synapse.analysis.depbump` as
labels. Those edges are ground truth -- a Go pseudo-version names the exact
upstream commit consumed -- so they can be held out and predicted.

Three questions, each answered with a standard statistic rather than an
impression:

**1. Ranking quality (AUC).** Rank every ordered repository pair by a measure and
ask: what is the probability that a true propagation edge outranks a
non-edge? That is the Mann-Whitney U statistic, equivalently the area under the
ROC curve. 0.5 is chance; 1.0 is perfect separation. Computed from ranks rather
than by sweeping thresholds, so it is exact and cheap.

**2. Precision@k.** Of the top k pairs a measure proposes, how many are real?
This is what actually matters to an agent, which will only ever look at a handful
of suggestions.

**3. Directional accuracy.** For a true edge A->B, does the measure score
``A->B`` above ``B->A``? This isolates whether the *direction* is recovered, not
merely the association. A symmetric measure scores exactly 0.5 here by
construction, which is the point: it is how the lagged construction earns its
complexity.

A caveat worth stating plainly: the labels only cover Go repositories that pin
internal modules by pseudo-version. A measure that scores well here is validated
for *declared dependency propagation*, not for the implicit coupling (Helm
charts, docs, configs) that has no manifest to be validated against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from git_synapse.db.engine import query
from git_synapse.stats.registry import ALL_KEYS, BY_KEY

log = logging.getLogger(__name__)


@dataclass
class MeasureScore:
    """How one measure performed at one lag."""

    measure: str
    lag_bins: int
    auc: float
    precision_at: dict[int, float] = field(default_factory=dict)
    directional_accuracy: float | None = None
    n_candidates: int = 0
    n_true: int = 0


def _auc(scores: np.ndarray, labels: np.ndarray) -> float:
    """Area under the ROC curve, via the rank-sum identity.

    ``AUC = (R_pos - n_pos(n_pos+1)/2) / (n_pos * n_neg)`` where ``R_pos`` is the
    sum of ranks of the positive class. Ties get average ranks, which is the
    correct handling and what ``scipy.stats.rankdata`` provides -- reimplemented
    here with argsort to avoid the dependency in a hot loop.
    """
    n = len(scores)
    n_pos = int(labels.sum())
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")

    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(n, dtype=np.float64)
    sorted_scores = scores[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        # Average rank for the tied block (ranks are 1-based).
        ranks[order[i : j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1

    rank_sum_pos = ranks[labels == 1].sum()
    return float((rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def ground_truth_edges(min_bumps: int = 2) -> set[tuple[int, int]]:
    """Ordered ``(dependency_repo, consumer_repo)`` pairs with observed bumps.

    ``min_bumps`` guards against a single opportunistic bump being treated as a
    standing dependency relationship.
    """
    rows = query(
        """
        SELECT dep_repo_id, consumer_repo_id
        FROM dep_bump
        WHERE dep_repo_id IS NOT NULL AND dep_repo_id <> consumer_repo_id
        GROUP BY 1, 2
        HAVING count(*) >= %(min_bumps)s
        """,
        {"min_bumps": max(min_bumps, 1)},
    )
    return {(int(r["dep_repo_id"]), int(r["consumer_repo_id"])) for r in rows}


def evaluate(
    lag_bins: int = 1,
    min_bumps: int = 2,
    ks: tuple[int, ...] = (10, 25, 50, 100),
    measures: tuple[str, ...] = ALL_KEYS,
) -> list[MeasureScore]:
    """Score every measure at one lag against the bump ground truth.

    Args:
        lag_bins: which lag of ``repo_lag_metric`` to evaluate.
        min_bumps: minimum bumps for an edge to count as a true label.
        ks: cut-offs for precision@k.
        measures: measures to evaluate.

    Returns:
        One :class:`MeasureScore` per measure, best AUC first.
    """
    truth = ground_truth_edges(min_bumps)
    if not truth:
        log.warning("no ground-truth edges; run `git-synapse depbump` first")
        return []

    cols = ", ".join(measures)
    rows = query(
        f"SELECT repo_a_id, repo_b_id, {cols} FROM repo_lag_metric WHERE lag_bins = %(lag)s",
        {"lag": lag_bins},
    )
    if not rows:
        log.warning("no lagged rows at lag %d; run `git-synapse lagged` first", lag_bins)
        return []

    pairs = [(int(r["repo_a_id"]), int(r["repo_b_id"])) for r in rows]
    labels = np.array([1 if p in truth else 0 for p in pairs], dtype=np.int8)

    # Index for the directional test: score of the reverse ordered pair.
    by_pair = {p: i for i, p in enumerate(pairs)}

    results: list[MeasureScore] = []
    for key in measures:
        raw = np.array(
            [(r[key] if r[key] is not None else 0.0) for r in rows], dtype=np.float64
        )
        raw = np.where(np.isfinite(raw), raw, 0.0)

        score = MeasureScore(
            measure=key,
            lag_bins=lag_bins,
            auc=_auc(raw, labels),
            n_candidates=len(pairs),
            n_true=int(labels.sum()),
        )

        order = np.argsort(-raw, kind="mergesort")
        for k in ks:
            top = order[:k]
            score.precision_at[k] = float(labels[top].mean()) if k else 0.0

        # Directional accuracy: only over true edges whose reverse is also a
        # candidate, otherwise the comparison is undefined.
        wins = comparable = 0
        for (a, b) in truth:
            i = by_pair.get((a, b))
            j = by_pair.get((b, a))
            if i is None or j is None:
                continue
            comparable += 1
            if raw[i] > raw[j]:
                wins += 1
            elif raw[i] == raw[j]:
                # A tie is no better than a coin flip; credit half.
                wins += 0.5
        score.directional_accuracy = (wins / comparable) if comparable else None
        results.append(score)

    results.sort(key=lambda s: (-(s.auc if s.auc == s.auc else -1)))
    return results


def sweep_lags(
    lags: tuple[int, ...] = (0, 1, 2, 3, 7, 14),
    measure: str = "npmi",
    min_bumps: int = 2,
) -> list[MeasureScore]:
    """Evaluate one measure across several lags, to find where signal peaks.

    The lag with the highest AUC is the corpus's characteristic propagation
    delay, recovered statistically rather than assumed.
    """
    out: list[MeasureScore] = []
    for lag in lags:
        scored = evaluate(lag_bins=lag, min_bumps=min_bumps, measures=(measure,))
        out.extend(scored)
    return out


def compare_to_symmetric(min_bumps: int = 2, measure: str = "npmi") -> dict:
    """Contrast the directed lagged table with the symmetric change-set table.

    This is the experiment that justifies the whole lagged construction: if the
    symmetric measure already recovered direction, the extra machinery would not
    be earning its place.
    """
    truth = ground_truth_edges(min_bumps)
    if not truth:
        return {}

    sym = query(
        f"""
        SELECT repo_a_id, repo_b_id, {measure} AS score, confidence_ab, confidence_ba
        FROM repo_pair_metric
        """
    )
    # The symmetric table stores one row per unordered pair, so expand it into
    # both orientations with the correct directional confidence for each.
    expanded: dict[tuple[int, int], float] = {}
    for r in sym:
        a, b = int(r["repo_a_id"]), int(r["repo_b_id"])
        expanded[(a, b)] = float(r["confidence_ab"] or 0.0)
        expanded[(b, a)] = float(r["confidence_ba"] or 0.0)

    wins = comparable = 0
    for (a, b) in truth:
        fwd, rev = expanded.get((a, b)), expanded.get((b, a))
        if fwd is None or rev is None:
            continue
        comparable += 1
        if fwd > rev:
            wins += 1
        elif fwd == rev:
            wins += 0.5

    return {
        "measure": measure,
        "symmetric_directional_accuracy": (wins / comparable) if comparable else None,
        "comparable_edges": comparable,
        "note": (
            "The symmetric table's only directional signal is the pair of "
            "conditional probabilities; it has no notion of time order."
        ),
    }
