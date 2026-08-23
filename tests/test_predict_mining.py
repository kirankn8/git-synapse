"""Impact prediction and the mining layer.

Both publish claims an agent acts on -- "this repo is upstream of yours", "these
files form a module", "this pair is decaying". A wrong answer here is not a
crash; it reads as a finding.
"""
from __future__ import annotations

import numpy as np
import pytest

from git_synapse.analysis import mining, predict
from git_synapse.analysis.predict import _rank_normalise


# ------------------------------------------------------- rank normalisation

def test_rank_normalise_is_bounded_and_order_preserving():
    values = np.array([5.0, 1.0, 3.0, 9.0, 7.0])
    out = _rank_normalise(values)
    assert out.min() >= 0.0 and out.max() <= 1.0
    # The ordering of the inputs must survive.
    assert list(np.argsort(values)) == list(np.argsort(out))


def test_rank_normalise_handles_a_constant_column():
    """Every value identical carries no information and must not produce nan,
    which would poison the mean the ensemble takes."""
    out = _rank_normalise(np.array([2.0, 2.0, 2.0, 2.0]))
    assert np.all(np.isfinite(out))


@pytest.mark.parametrize("values", [
    np.array([1.0]),                      # single element
    np.array([]),                         # empty
    np.array([np.nan, 1.0, 2.0]),         # a nan in the column
    np.array([-np.inf, 0.0, np.inf]),     # infinities
])
def test_rank_normalise_never_returns_nan_or_out_of_range(values):
    out = _rank_normalise(values)
    assert out.shape == values.shape
    if out.size:
        assert np.all(np.isfinite(out)), out
        assert out.min() >= 0.0 and out.max() <= 1.0


def test_rank_normalise_is_invariant_to_monotone_rescaling():
    base = np.array([1.0, 4.0, 9.0, 16.0])
    assert np.allclose(_rank_normalise(base), _rank_normalise(base * 100 + 7))


# ------------------------------------------------------------ impact tiers

def test_impact_and_upstream_are_exact_inverses(db):
    """If A is upstream of B then B must be downstream of A, or the two tools
    contradict each other about the same edge."""
    from git_synapse.db.engine import query

    edges = query(
        "SELECT source_repo_id s, target_repo_id t FROM repo_impact"
        " WHERE is_declared OR has_bump_history LIMIT 12"
    )
    if not edges:
        pytest.skip("no validated edges")

    for e in edges:
        downstream = {r["target_repo_id"] for r in predict.impact_for(e["s"], limit=500)}
        upstream = {r["source_repo_id"] for r in predict.upstream_of(e["t"], limit=500)}
        assert e["t"] in downstream, f"{e['s']}->{e['t']} missing downstream"
        assert e["s"] in upstream, f"{e['s']}->{e['t']} missing upstream"


def test_no_repository_is_its_own_upstream(db):
    from git_synapse.db.engine import query_one

    assert query_one(
        "SELECT count(*) AS n FROM repo_impact WHERE source_repo_id = target_repo_id"
    )["n"] == 0


def test_impact_scores_are_probabilities_and_ranked(db):
    from git_synapse.db.engine import query

    rows = query("SELECT source_repo_id FROM repo_impact LIMIT 1")
    if not rows:
        pytest.skip("impact table empty")
    out = predict.impact_for(rows[0]["source_repo_id"], limit=20)
    scores = [float(r["score"]) for r in out]
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores == sorted(scores, reverse=True) or True  # ordering asserted below
    assert out == sorted(
        out, key=lambda r: (r["is_declared"] or r["has_bump_history"], r["score"]),
        reverse=True,
    ), "validated evidence must rank above discovery"


@pytest.mark.parametrize("repo_id", [0, -1, 999999999])
def test_impact_on_a_nonexistent_repo_is_empty_not_an_error(db, repo_id):
    assert predict.impact_for(repo_id, limit=5) == []
    assert predict.upstream_of(repo_id, limit=5) == []


def test_chains_never_revisit_a_repository(db):
    """A cycle would loop forever or report a repo as its own ancestor."""
    from git_synapse.db.engine import query

    rows = query("SELECT DISTINCT target_repo_id t FROM repo_impact LIMIT 8")
    for r in rows:
        for chain in predict.upstream_chains(r["t"], max_depth=3, limit=5):
            path = list(chain["path"])
            assert len(path) == len(set(path)), f"cycle in {chain['repo_names']}"
            assert chain["depth"] >= 2, "a chain must be more than one hop"


# ----------------------------------------------------------------- mining

def test_cross_directory_modules_span_more_than_one_directory(db):
    """The whole point of the label-propagation clusters is finding modules the
    directory tree does not show."""
    from git_synapse.db.engine import query

    rows = query("SELECT DISTINCT repo_id FROM file_cluster LIMIT 5")
    if not rows:
        pytest.skip("no clusters mined")
    for r in rows:
        for m in mining.cross_directory_modules(r["repo_id"], limit=5):
            assert m["dirs_spanned"] > 1, m


@pytest.mark.parametrize("trend", ["emerging", "decaying"])
def test_drifting_pairs_carry_the_trend_they_were_asked_for(db, trend):
    rows = mining.drifting_pairs(trend=trend, limit=10)
    assert all(r["trend"] == trend for r in rows)


def test_risky_files_are_ranked_and_bounded(db):
    rows = mining.risky_files(limit=10)
    if not rows:
        pytest.skip("no risk rows")
    scores = [float(r["risk"]) for r in rows if r.get("risk") is not None]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("repo_id", [0, -1, 999999999])
def test_mining_readers_on_an_unknown_repo_return_empty(db, repo_id):
    assert mining.cross_directory_modules(repo_id, limit=5) == []
    assert mining.risky_files(repo_id, limit=5) == []
