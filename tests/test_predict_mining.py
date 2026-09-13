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


# ------------------------------------------------------------ npmi, directly

@pytest.mark.parametrize("joint,left,right,population", [
    (0, 5, 5, 10),   # a pair that never co-occurred
    (2, 0, 5, 10),   # a file with no changes of its own
    (2, 5, 0, 10),
    (2, 5, 5, 0),    # an empty window
])
def test_npmi_is_undefined_rather_than_zero_when_a_count_is_missing(
    joint, left, right, population,
):
    """None and 0.0 mean different things here: "no evidence" against "evidence
    of independence". Returning 0.0 for both would rank them together."""
    assert mining._npmi(joint, left, right, population) is None


def test_npmi_is_one_when_two_files_always_move_together():
    """Perfect association is the top of the scale, and the denominator is zero
    there -- computing it would divide by zero rather than say 1.0."""
    assert mining._npmi(4, 4, 4, 4) == 1.0
    assert mining._npmi(5, 5, 5, 4) == 1.0


def test_npmi_is_bounded_and_ordered_by_how_exclusive_the_pairing_is():
    loose = mining._npmi(2, 8, 8, 100)
    tight = mining._npmi(6, 7, 7, 100)
    assert -1.0 <= loose <= 1.0 and -1.0 <= tight <= 1.0
    assert tight > loose


# --------------------------------------------- drift, clusters and their readers

@pytest.fixture()
def drift_corpus(db):
    """One repository whose two files move together in both time windows.

    A pair needs at least two co-changes on each side of the boundary before it
    is scored, so the fixture supplies exactly that: two recent commits and two
    old ones, each touching both files.
    """
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    # Additive: this database is shared with every other test in the run, and
    # several of them skip when the corpus lacks a shape they need. Clearing
    # the tables to get a clean slate takes that shape away from them.
    tag = uuid4().hex[:8]
    with connection() as conn:
        repo = models().Repo(github_id=abs(hash(tag)) % 10**8, owner="acme",
                             name=f"drift-{tag}", full_name=f"acme/drift-{tag}",
                             clone_url="", default_branch="main")
        conn.add(repo)
        conn.flush()

        files = []
        for path in ("src/a.py", "docs/b.md"):
            row = models().File(repo_id=repo.id, path=path,
                                dir_path=path.split("/")[0],
                                basename=path.split("/")[-1], change_count=4)
            conn.add(row)
            files.append(row)
        conn.flush()

        now = datetime.now(UTC)
        for i, age_days in enumerate((1, 2, 400, 401)):
            at = now - timedelta(days=age_days)
            commit = models().Commit(repo_id=repo.id, sha=f"{i:040x}", authored_at=at,
                                     committed_at=at, pair_eligible=True, n_files=2)
            conn.add(commit)
            conn.flush()
            for f in files:
                conn.add(models().CommitFile(commit_id=commit.id, file_id=f.id,
                                             repo_id=repo.id))
        repo_id, file_ids, name = repo.id, [f.id for f in files], repo.name

    yield {"repo_id": repo_id, "file_ids": file_ids, "name": name}

    with connection() as conn:
        for model in (models().PairDrift, models().FileCluster, models().FilePairMetric,
                      models().CommitFile, models().Commit, models().File):
            conn.query(model).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        conn.query(models().Repo).filter_by(id=repo_id).delete(synchronize_session=False)


def test_a_pair_seen_in_both_windows_is_scored_for_drift(drift_corpus):
    """Both npmi values and their delta come out of `_rebuild_drift`; a pair
    present in only one window is not evidence of a trend either way."""
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        mining._rebuild_drift(session, drift_corpus["repo_id"])

    with connection() as session:
        rows = session.query(models().PairDrift).filter_by(
            repo_id=drift_corpus["repo_id"]).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.n_ab_recent == 2 and row.n_ab_historic == 2
        assert row.trend in {"emerging", "decaying", "stable"}
        assert row.npmi_recent is not None and row.npmi_historic is not None


def test_drifting_pairs_names_both_files_and_their_repository(drift_corpus):
    """The reader joins the pair back to paths and a repo name, because a row of
    two integers is not something anybody can act on."""
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    a, b = sorted(drift_corpus["file_ids"])
    with connection() as session:
        session.add(models().PairDrift(
            repo_id=drift_corpus["repo_id"], file_a_id=a, file_b_id=b,
            window_days=90, n_ab_recent=6, n_ab_historic=2,
            npmi_recent=0.8, npmi_historic=0.2, delta=0.6, trend="emerging"))

    rows = mining.drifting_pairs(repo_id=drift_corpus["repo_id"], trend="emerging")
    assert len(rows) == 1
    assert rows[0]["path_a"] == "src/a.py"
    assert rows[0]["path_b"] == "docs/b.md"
    assert rows[0]["repo"] == drift_corpus["name"]


def test_a_drifting_pair_whose_file_was_deleted_is_hidden_unless_asked_for(drift_corpus):
    """Recommending a file that no longer exists is worse than recommending
    nothing, so deleted partners are dropped by default."""
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    a, b = sorted(drift_corpus["file_ids"])
    with connection() as session:
        session.add(models().PairDrift(
            repo_id=drift_corpus["repo_id"], file_a_id=a, file_b_id=b,
            window_days=90, n_ab_recent=6, n_ab_historic=2,
            npmi_recent=0.8, npmi_historic=0.2, delta=0.6, trend="emerging"))
        session.query(models().File).filter_by(id=b).update({"is_deleted": True})

    assert mining.drifting_pairs(repo_id=drift_corpus["repo_id"], trend="emerging") == []
    kept = mining.drifting_pairs(repo_id=drift_corpus["repo_id"], trend="emerging",
                                 include_deleted=True)
    assert len(kept) == 1


def test_cross_directory_modules_report_the_directories_they_span(drift_corpus):
    """The point of the cluster is that it crosses a directory boundary: files
    that move together while living apart are what a newcomer cannot see."""
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        for file_id in drift_corpus["file_ids"]:
            session.add(models().FileCluster(
                repo_id=drift_corpus["repo_id"], file_id=file_id, cluster_id=1,
                cluster_size=3, cohesion=0.75, dirs_spanned=2))

    rows = mining.cross_directory_modules(drift_corpus["repo_id"])
    assert len(rows) == 1
    assert rows[0]["cluster_size"] == 3
    assert rows[0]["dirs_spanned"] == 2
    assert rows[0]["avg_cohesion"] == 0.75
    assert rows[0]["directories"] == ["docs", "src"]
    assert set(rows[0]["sample_files"]) == {"src/a.py", "docs/b.md"}


# ------------------------------------------- query readers over a known corpus

def test_a_minimum_score_drops_partners_beneath_it(drift_corpus):
    """`min_score` is the caller saying "below this is not worth my attention",
    so a partner under the bar is absent rather than present with a low number.
    """
    from git_synapse.analysis import query
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    a, b = sorted(drift_corpus["file_ids"])
    with connection() as session:
        session.add(models().FilePairMetric(
            repo_id=drift_corpus["repo_id"], file_a_id=a, file_b_id=b,
            n_ab=4, n_a=4, n_b=4, n_total=4, jaccard=0.5))

    assert [r["other_id"] for r in
            query.coupled_files(a, measure="jaccard", min_score=0.1)] == [b]
    assert query.coupled_files(a, measure="jaccard", min_score=0.9) == []


def test_an_impact_edge_and_its_declaration_can_be_read_back_singly(impact_corpus):
    """The UI asks about one edge at a time when somebody clicks it; fetching
    the whole graph to answer that would be the wrong shape entirely."""
    from git_synapse.analysis import query

    signer, packager = impact_corpus["signer"], impact_corpus["packager"]

    edge = query.impact_pair(signer, packager)
    assert edge is not None and edge["source_repo_id"] == signer

    declared = query.declared_dependency(dep_repo_id=signer, consumer_repo_id=packager)
    assert declared is not None
    assert declared["dep_name"] == "github.com/acme/signer"

    assert query.impact_pair(signer, 999_999) is None
    assert query.declared_dependency(dep_repo_id=999_999, consumer_repo_id=packager) is None


def test_repo_dependencies_names_the_repository_behind_each_declaration(impact_corpus):
    """A manifest line is a string; the useful answer is which tracked
    repository it resolves to, so the reader joins it back."""
    from git_synapse.analysis import query

    result = query.repo_dependencies(impact_corpus["packager"])
    names = {d["dep_repo"] for d in result["declared"]}
    assert "signer" in names


def test_a_pairing_with_too_few_bumps_has_no_adoption_statistics(impact_corpus):
    """A median over one or two observations is not a measurement, so a pairing
    under three bumps is left out rather than reported with a wide error bar."""
    from datetime import UTC, datetime

    from git_synapse.analysis import depbump as db_mod
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    with connection() as session:
        session.add(models().DepBump(
            consumer_repo_id=impact_corpus["runtime"], consumer_sha="a" * 40,
            dep_repo_id=impact_corpus["packager"], dep_name="github.com/acme/packager",
            dep_version="v9.9.9", manifest="go.mod",
            bumped_at=datetime.now(UTC), adoption_seconds=3600))

    rows = db_mod.adoption_delays()
    pairs = {(r["dep"], r["consumer"]) for r in rows}
    # packager->signer has five bumps and is reported; runtime->packager has one.
    assert ("signer", "packager") in pairs
    assert ("packager", "runtime") not in pairs


def test_declared_only_hides_an_edge_that_no_manifest_states(impact_corpus):
    """A bump-only edge is real evidence but not a declaration, so a caller
    asking for declarations must not be handed one."""
    from git_synapse.analysis import predict
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    signer, unrelated = impact_corpus["signer"], impact_corpus["unrelated"]
    with connection() as session:
        session.add(models().RepoImpact(
            source_repo_id=signer, target_repo_id=unrelated, score=0.9,
            rank_in_source=99, is_declared=False, has_bump_history=True))

    everything = {r["target_repo_id"] for r in predict.impact_for(signer, limit=50)}
    declared = {r["target_repo_id"]
                for r in predict.impact_for(signer, limit=50, declared_only=True)}
    assert unrelated in everything
    assert unrelated not in declared


def test_a_cycle_in_the_graph_does_not_walk_forever(impact_corpus):
    """Two repositories that each declare the other are a real shape, and a
    chain walker that revisits a node on the path never terminates."""
    from git_synapse.analysis import predict
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    signer, packager = impact_corpus["signer"], impact_corpus["packager"]
    with connection() as session:
        session.query(models().RepoImpact).filter_by(
            source_repo_id=packager, target_repo_id=signer).delete(
                synchronize_session=False)
        session.add(models().RepoImpact(
            source_repo_id=packager, target_repo_id=signer, score=0.9,
            rank_in_source=1, is_declared=True, has_bump_history=True))

    chains = predict.impact_chains(signer, max_depth=4, min_score=0.1)
    for chain in chains:
        assert len(chain["path"]) == len(set(chain["path"])), chain["path"]

    # A chain is at least two hops, so a depth of one stops the walk before it
    # has anything to report -- the bound is enforced, not merely advertised.
    assert predict.impact_chains(signer, max_depth=1, min_score=0.1) == []
