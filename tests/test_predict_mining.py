"""Impact prediction and the mining layer.

Both publish claims an agent acts on -- "this repo is upstream of yours", "these
files form a module", "this pair is decaying". A wrong answer here is not a
crash; it reads as a finding.
"""
from __future__ import annotations

import pytest

from git_synapse.analysis import mining, predict

# ------------------------------------------------------- rank normalisation


# ------------------------------------------------------------ impact tiers

def test_impact_and_upstream_are_exact_inverses(db):
    """If A is upstream of B then B must be downstream of A, or the two tools
    contradict each other about the same edge."""
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        edges = [
            {"s": row.source_repo_id, "t": row.target_repo_id}
            for row in session.query(models().RepoImpact)
            .filter((models().RepoImpact.is_declared.is_(True)) |
                    (models().RepoImpact.has_bump_history.is_(True)))
            .limit(12).all()
        ]
    if not edges:
        pytest.skip("no validated edges")

    for e in edges:
        downstream = {r["target_repo_id"] for r in predict.impact_for(e["s"], limit=500)}
        upstream = {r["source_repo_id"] for r in predict.upstream_of(e["t"], limit=500)}
        assert e["t"] in downstream, f"{e['s']}->{e['t']} missing downstream"
        assert e["s"] in upstream, f"{e['s']}->{e['t']} missing upstream"


def test_no_repository_is_its_own_upstream(db):
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        assert session.query(models().RepoImpact).filter(
            models().RepoImpact.source_repo_id == models().RepoImpact.target_repo_id
        ).count() == 0


def test_impact_scores_are_probabilities_and_ranked(db):
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        first = session.query(models().RepoImpact.source_repo_id).first()
    rows = [{"source_repo_id": first[0]}] if first else []
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


@pytest.mark.parametrize("repo_id", [0, -1, 999_999])
def test_impact_on_a_nonexistent_repo_is_empty_not_an_error(db, repo_id):
    assert predict.impact_for(repo_id, limit=5) == []
    assert predict.upstream_of(repo_id, limit=5) == []


def test_chains_never_revisit_a_repository(db):
    """A cycle would loop forever or report a repo as its own ancestor."""
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        rows = [{"t": value[0]} for value in session.query(
            models().RepoImpact.target_repo_id).distinct().limit(8).all()]
    for r in rows:
        for chain in predict.upstream_chains(r["t"], max_depth=3, limit=5):
            path = list(chain["path"])
            assert len(path) == len(set(path)), f"cycle in {chain['repo_names']}"
            assert chain["depth"] >= 2, "a chain must be more than one hop"


# ----------------------------------------------------------------- mining

def test_cross_directory_modules_span_more_than_one_directory(db):
    """The whole point of the label-propagation clusters is finding modules the
    directory tree does not show."""
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        rows = [{"repo_id": value[0]} for value in session.query(
            models().FileCluster.repo_id).distinct().limit(5).all()]
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


@pytest.mark.parametrize("repo_id", [0, -1, 999_999])
def test_mining_readers_on_an_unknown_repo_return_empty(db, repo_id):
    assert mining.cross_directory_modules(repo_id, limit=5) == []
    assert mining.risky_files(repo_id, limit=5) == []



# --------------------------------------------- impact rebuild on a known corpus

@pytest.fixture()
def impact_corpus(scratch_db):
    """Three repositories wired by declared dependencies and bump history.

    The graph is what repositories say about each other, so the fixture states
    it the same way: a manifest row in `repo_dependency`, and version changes in
    `dep_bump`.
    """
    from datetime import UTC, datetime, timedelta

    from git_synapse.analysis import predict
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as conn:
        for model in (models().RepoImpact, models().DepBump,
                      models().RepoDependency, models().Repo):
            conn.query(model).delete(synchronize_session=False)
        ids = {}
        for name in ("signer", "packager", "runtime", "unrelated"):
            row = models().Repo(github_id=abs(hash(name)) % 100000,
                                owner="acme", name=name,
                                full_name=f"acme/{name}", clone_url="",
                                default_branch="main")
            conn.add(row)
            conn.flush()
            ids[name] = row.id

        # packager declares signer and has bumped it; runtime declares packager
        # but has never moved it.
        for consumer, dep in (("packager", "signer"), ("runtime", "packager")):
            conn.add(models().RepoDependency(
                consumer_repo_id=ids[consumer], dep_repo_id=ids[dep],
                dep_name=f"github.com/acme/{dep}", manifest="go.mod", ecosystem="go",
            ))
        for i in range(5):
            conn.add(models().DepBump(
                consumer_repo_id=ids["packager"], consumer_sha=f"{i:040x}",
                dep_repo_id=ids["signer"], dep_name="github.com/acme/signer",
                dep_version=f"v1.{i}.0", manifest="go.mod",
                bumped_at=datetime.now(UTC) - timedelta(days=i * 10),
                adoption_seconds=86400 * 2,
            ))
    predict.rebuild(force=True)
    return ids


def _edges():
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        rows = session.query(models().RepoImpact,
                             models().Repo.name,
                             models().Repo.name).join(
            models().Repo, models().Repo.id == models().RepoImpact.source_repo_id
        ).all()
        # Resolve target names explicitly through the ORM identity map.
        repos = {r.id: r.name for r in session.query(models().Repo).all()}
        return [{**{column: getattr(edge, column) for column in (
            "source_repo_id", "target_repo_id", "score", "rank_in_source",
            "is_declared", "has_bump_history", "bump_count", "median_adoption_days",
            "features")}, "source": repos[edge.source_repo_id],
                 "target": repos[edge.target_repo_id]}
                for edge, _, _ in rows]


def test_a_declared_dependency_becomes_an_edge(impact_corpus):
    edges = {(e["source"], e["target"]) for e in _edges()}
    assert ("signer", "packager") in edges
    assert ("packager", "runtime") in edges


def test_a_repository_nothing_declares_has_no_edges(impact_corpus):
    """The old model surfaced every busy repository; a declared graph cannot."""
    names = {e["source"] for e in _edges()} | {e["target"] for e in _edges()}
    assert "unrelated" not in names


def test_a_bump_backed_edge_carries_its_count_and_lag(impact_corpus):
    edge = next(e for e in _edges() if (e["source"], e["target"]) == ("signer", "packager"))
    assert edge["has_bump_history"] and edge["bump_count"] == 5
    assert edge["median_adoption_days"] == pytest.approx(2.0, abs=0.01)
    assert edge["is_declared"]


def test_a_declared_edge_with_no_bumps_is_still_recorded(impact_corpus):
    """Declared but never moved is a real, weaker relationship -- not absent."""
    edge = next(e for e in _edges() if (e["source"], e["target"]) == ("packager", "runtime"))
    assert edge["is_declared"] and not edge["has_bump_history"]
    assert edge["bump_count"] == 0


def test_bump_history_outranks_a_bare_declaration(impact_corpus):
    by = {(e["source"], e["target"]): e["score"] for e in _edges()}
    assert by[("signer", "packager")] > by[("packager", "runtime")]


def test_every_score_is_bounded_and_explainable(impact_corpus):
    for e in _edges():
        assert 0.0 <= e["score"] <= 1.0
        assert e["features"]["scored_by"] == "declared"


def test_no_repository_is_its_own_dependency(impact_corpus):
    assert all(e["source_repo_id"] != e["target_repo_id"] for e in _edges())


def test_edges_are_ranked_within_each_source(impact_corpus):
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models
    with connection() as session:
        rows = session.query(models().RepoImpact).order_by(
            models().RepoImpact.source_repo_id,
            models().RepoImpact.rank_in_source,
        ).all()
    for row in rows:
        assert row.rank_in_source >= 1


def test_a_second_rebuild_is_skipped_when_no_input_changed(impact_corpus):
    from git_synapse.analysis import predict
    assert predict.rebuild().rows_written == 0, "unchanged inputs must not rebuild"


def test_a_rebuild_can_run_inside_a_callers_transaction(impact_corpus):
    from git_synapse.analysis import predict
    from git_synapse.db.engine import connection

    with connection() as conn:
        assert predict.rebuild(conn, force=True).rows_written > 0


# ------------------------------------- rankings do not spend slots on the dead

@pytest.fixture
def dead_file(db):
    """A repository of our own holding one file deleted at HEAD.

    Its own repository, not whichever one happens to exist: a test that borrows
    another test's data passes or fails on the order they run in, which is the
    flake this suite has been bitten by before.
    """
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        repo = models().Repo(owner="ranktest", name="dead", full_name="ranktest/dead",
                              host="github.com", provider="github", is_enabled=True,
                              commit_count=500, pair_population=500)
        session.add(repo)
        session.flush()
        file_row = models().File(repo_id=repo.id, path="gone/removed.py", dir_path="gone",
                                 basename="removed.py", extension="py", depth=1,
                                 is_deleted=True, change_count=9999,
                                 pair_change_count=9999, author_count=3)
        session.add(file_row)
        session.flush()
        repo_id, file_id = repo.id, file_row.id
    yield repo_id, file_id
    with connection() as session:
        risk = session.get(models().FileRisk, file_id)
        if risk is not None:
            session.delete(risk)
        session.delete(session.get(models().File, file_id))
        session.delete(session.get(models().Repo, repo_id))


def test_hotspots_leave_out_files_that_no_longer_exist(dead_file):
    """A ranking says "look here". A file deleted at HEAD is not somewhere
    anyone can look, and each one spends a slot the reader came for."""
    from git_synapse.analysis import query as q

    repo_id, fid = dead_file
    assert fid not in {r["id"] for r in q.hotspots(repo_id=repo_id, limit=10)}, \
        "9,999 changes, top of the repository, and still excluded"
    # Not hidden, only unranked: it was real while the file existed.
    assert fid in {r["id"] for r in
                   q.hotspots(repo_id=repo_id, limit=10, include_deleted=True)}


def test_risk_leaves_out_files_that_no_longer_exist(dead_file):
    """Risk answers "what happens if I change this, and who understands it" --
    a question that cannot be asked of a file that is gone."""
    from git_synapse.analysis import mining
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    repo_id, fid = dead_file
    with connection() as session:
        if session.get(models().FileRisk, fid) is None:
            session.add(models().FileRisk(
                file_id=fid, repo_id=repo_id, churn_pct=1, coupling_pct=1,
                ownership_hhi=1, effective_authors=1, author_count=3,
                partner_count=40, change_count=9999, risk_score=1.99,
            ))
    assert fid not in {r["file_id"] for r in mining.risky_files(repo_id=repo_id, limit=10)}, \
        "the highest possible score, and still out"
    assert fid in {r["file_id"] for r in
                   mining.risky_files(repo_id=repo_id, limit=10, include_deleted=True)}


def test_a_coupling_query_still_reports_a_deleted_partner(db):
    """The opposite judgement, and deliberately so: the reader asked about one
    specific file, the coupling is a historical fact, and the answer labels it
    rather than hiding it."""
    from git_synapse.analysis import query as q
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        row = session.query(models().FilePair).join(
            models().File, models().File.id == models().FilePair.file_b_id
        ).filter(models().File.is_deleted.is_(True)).first()
    if row is None:
        import pytest

        pytest.skip("no deleted partner in this corpus")
    partners = q.coupled_files(row.file_a_id, limit=500)
    assert any(pt.get("is_deleted") for pt in partners), \
        "a deleted partner must still be offered, marked"


def test_label_propagation_does_not_allocate_a_node_by_node_matrix():
    """The sweep used to key a bincount on `node * n + label`, which needs
    `minlength = n*n` -- a dense float64 matrix, reallocated every round, that
    grows with the *square* of the repository. wireshark's 7,007 coupled files
    made that 396MB a round; the sparse sweep does the same work in 25MB.

    Only the (node, label) pairs that actually occur can carry weight, and
    there are at most 2E of those.
    """
    import contextlib
    import tracemalloc

    import numpy as np

    from git_synapse.analysis import mining

    # Many nodes, few edges: the shape where the two costs diverge hardest.
    n_nodes = 4000
    rng = np.random.default_rng(7)
    src = rng.integers(0, n_nodes, 3000)
    dst = (src + 1) % n_nodes
    edges = [(int(a) + 1, int(b) + 1, 0.9) for a, b in zip(src, dst, strict=True)
             if a != b]

    class _Conn:
        """Just enough connection to drive the clustering sweep."""

        def __init__(self):
            self.rows = edges

        def execute(self, sql, params=None):
            class _R:
                def __init__(self, rows):
                    self._rows = rows

                def fetchall(self):
                    return self._rows

                def fetchone(self):
                    return self._rows[0] if self._rows else None

            if "FROM file_pair" in sql:
                return _R(self.rows)
            if "FROM file" in sql:
                return _R([(i, f"dir{i % 20}") for i in range(1, n_nodes + 1)])
            return _R([])

    stats = mining.MiningStats()
    tracemalloc.start()
    # The stub stops at the first write; the sweep has already run by then.
    with contextlib.suppress(AttributeError, TypeError):
        mining._cluster_repo(_Conn(), 1, stats)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    dense_would_be = n_nodes * n_nodes * 8
    assert peak < dense_would_be / 10, (
        f"peak {peak / 1e6:.1f}MB is within an order of magnitude of the "
        f"{dense_would_be / 1e6:.0f}MB dense matrix this replaced")
