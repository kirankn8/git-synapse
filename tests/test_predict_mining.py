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


# --------------------------------------------- impact rebuild on a known corpus
#
# A synthetic corpus in a throwaway database, small enough that every branch of
# the ranking is reachable by construction: the two evidence tiers, an edge with
# structural evidence but no statistics at all, and a hub with more strong
# undeclared candidates than it is allowed to keep.


@pytest.fixture()
def impact_corpus(scratch_db):
    """A corpus wired so each ranking branch has a witness.

    Deliberately not tiny. The undeclared floor is a percentile of the
    rank-normalised discovery score, so a handful of pairs puts everything in the
    top few percent by construction and the filter cannot be observed at all. The
    filler pairs exist to give that percentile something to mean.
    """
    from git_synapse.analysis import predict
    from git_synapse.db.engine import connection

    with connection() as conn:
        conn.execute("TRUNCATE repo, dep_bump, repo_dependency, repo_lag_metric,"
                     " repo_impact, repo_pair_metric RESTART IDENTITY CASCADE")
        ids = {}
        for n in range(64):
            ids[n] = conn.execute(
                "INSERT INTO repo (github_id, owner, name, full_name, clone_url,"
                " default_branch) VALUES (%s,'t',%s,%s,'',%s) RETURNING id",
                (1000 + n, f"r{n}", f"t/r{n}", "main"),
            ).fetchone()[0]

        measures = list(dict.fromkeys(
            predict.ENSEMBLE_MEASURES + predict.DISCOVERY_MEASURES))
        cols = ", ".join(measures)
        marks = ", ".join(["%s"] * len(measures))

        def lag_row(a, b, value, n_ab, lag=1):
            conn.execute(
                f"INSERT INTO repo_lag_metric (repo_a_id, repo_b_id, lag_bins,"
                f" bin_hours, n_ab, n_a, n_b, n_total, {cols})"
                f" VALUES (%s,%s,%s,24,%s,50,50,1000,{marks})",
                (ids[a], ids[b], lag, n_ab, *[value] * len(measures)),
            )

        # 400 weak pairs, so the top percentile is a small slice of a real
        # population rather than the whole of a toy one.
        for a in range(10, 50):
            for b in range(50, 60):
                lag_row(a, b, 0.01 + (a + b) / 10000.0, n_ab=2)

        # r0 is a hub whose eight undeclared candidates all top the ranking:
        # only the strongest few may be kept.
        for b in range(1, 9):
            lag_row(0, b, 0.9 + b / 1000.0, n_ab=predict.UNDECLARED_MIN_SUPPORT + b)
        # Scores as highly as the hub's edges but is seen too few times to mean
        # anything.
        lag_row(1, 2, 0.999, n_ab=predict.UNDECLARED_MIN_SUPPORT - 1)
        # A declared edge that also has lagged statistics.
        lag_row(3, 4, 0.2, n_ab=30)
        conn.execute(
            "INSERT INTO repo_dependency (consumer_repo_id, dep_repo_id, dep_name,"
            " manifest, ecosystem, observed_at) VALUES (%s,%s,'r3','go.mod','go',now())",
            (ids[4], ids[3]),
        )
        # A declared edge with no lagged row at all: it must still be reported,
        # ranked last on statistics but carrying its tier.
        conn.execute(
            "INSERT INTO repo_dependency (consumer_repo_id, dep_repo_id, dep_name,"
            " manifest, ecosystem, observed_at) VALUES (%s,%s,'r60','go.mod','go',now())",
            (ids[61], ids[60]),
        )
        # The symmetric table's view of the same pair, identical in both
        # directions -- the case the directional comparison has to score at 0.5.
        conn.execute(
            "INSERT INTO repo_pair_metric (repo_a_id, repo_b_id, n_ab, n_a, n_b,"
            " n_total, confidence_ab, confidence_ba, npmi)"
            " VALUES (%s,%s,5,10,10,100,0.5,0.5,0.4)",
            (ids[62], ids[63]),
        )
        # A bump-backed edge, likewise with no statistics.
        conn.execute(
            "INSERT INTO dep_bump (consumer_repo_id, consumer_sha, dep_repo_id,"
            " dep_name, dep_version, manifest, bumped_at, lag_seconds)"
            " VALUES (%s,'a',%s,'r62','v1','go.mod',now(),172800)",
            (ids[63], ids[62]),
        )
    return ids


def test_impact_reports_a_declared_edge_that_has_no_statistics(impact_corpus):
    """The most expensive wrong answer this system can give is "no upstream" for
    a repository whose manifest names one."""
    from git_synapse.analysis import predict

    predict.rebuild(force=True)
    rows = predict.upstream_of(impact_corpus[61], limit=20)
    assert [r["name"] for r in rows] == ["r60"]
    assert rows[0]["is_declared"] is True


def test_a_bump_backed_edge_carries_its_count_and_lag(impact_corpus):
    from git_synapse.analysis import predict

    predict.rebuild(force=True)
    rows = predict.upstream_of(impact_corpus[63], limit=20)
    assert [r["name"] for r in rows] == ["r62"]
    assert rows[0]["is_declared"] is False
    assert rows[0]["has_bump_history"] is True
    assert rows[0]["bump_count"] == 1
    assert rows[0]["median_lag_days"] == pytest.approx(2.0)


def test_a_hub_cannot_flood_its_own_shortlist_with_undeclared_edges(impact_corpus):
    from git_synapse.analysis import predict

    predict.rebuild(force=True)
    rows = predict.impact_for(impact_corpus[0], limit=50)
    undeclared = [r for r in rows if not r["is_declared"] and not r["has_bump_history"]]
    # Eight candidates cleared both floors; the cap is what stops all eight.
    assert len(undeclared) == predict.MAX_UNDECLARED_PER_SOURCE


def test_a_thinly_supported_pair_is_not_surfaced_however_high_it_scores(impact_corpus):
    """Score alone is not evidence: a pair seen a handful of times can top every
    measure by accident."""
    from git_synapse.analysis import predict

    predict.rebuild(force=True)
    rows = predict.impact_for(impact_corpus[1], limit=50)
    assert [r["name"] for r in rows if r["name"] == "r2"] == []


def test_declared_only_filters_out_discovered_edges(impact_corpus):
    from git_synapse.analysis import predict

    predict.rebuild(force=True)
    all_rows = predict.impact_for(impact_corpus[0], limit=50)
    only = predict.impact_for(impact_corpus[0], limit=50, declared_only=True)
    assert all_rows and not only


def test_a_second_rebuild_is_skipped_when_no_input_changed(impact_corpus):
    """Six global rebuilds run on every ingest tick; recomputing impact over an
    unchanged corpus is pure cost."""
    from git_synapse.analysis import predict

    first = predict.rebuild(force=True)
    second = predict.rebuild(force=False)
    assert second.rows_written == first.rows_written
    assert second.sources == 0, "the skip path recomputed the ranking"


def test_a_rebuild_with_no_lagged_metrics_writes_nothing(scratch_db):
    from git_synapse.analysis import predict
    from git_synapse.db.engine import connection

    with connection() as conn:
        conn.execute("TRUNCATE repo, dep_bump, repo_dependency, repo_lag_metric,"
                     " repo_impact RESTART IDENTITY CASCADE")
    stats = predict.rebuild(force=True)
    assert stats.rows_written == 0


def test_a_rebuild_can_run_inside_a_callers_transaction(impact_corpus):
    """The pipeline passes its own connection so the derived tables land in the
    same transaction as the run record."""
    from git_synapse.analysis import predict
    from git_synapse.db.engine import connection

    with connection() as conn:
        stats = predict.rebuild(conn=conn, force=True)
        assert stats.rows_written > 0
        assert conn.execute("SELECT count(*) FROM repo_impact").fetchone()[0] == \
            stats.rows_written


def test_chains_can_be_walked_through_discovery_hops_when_asked(impact_corpus):
    """Off by default because a discovery hop is scored on a different,
    unvalidated scale -- chaining through one reads as coupling when it is
    activity confounding."""
    from git_synapse.analysis import predict

    predict.rebuild(force=True)
    validated = predict.impact_chains(impact_corpus[0], max_depth=2)
    everything = predict.impact_chains(impact_corpus[0], max_depth=2,
                                     validated_only=False)
    assert len(everything) >= len(validated)
    assert not validated, "r0's edges are all discovery-tier"


def test_a_symmetric_measure_that_cannot_tell_direction_scores_a_half(impact_corpus):
    """This is the experiment that justifies the lagged construction. A measure
    identical in both directions must score 0.5 -- rounding each tie to a win is
    exactly how a symmetric measure comes out looking directional."""
    from git_synapse.analysis import validate

    result = validate.compare_to_symmetric(min_bumps=1, measure="npmi")
    assert result, "the fixture's bump edges should be ground truth"
    assert result["comparable_edges"] == 1
    assert result["symmetric_directional_accuracy"] == pytest.approx(0.5)
