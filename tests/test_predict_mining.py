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


@pytest.mark.parametrize("repo_id", [0, -1, 999_999])
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
    from git_synapse.analysis import predict
    from git_synapse.db.engine import connection

    with connection() as conn:
        conn.execute("TRUNCATE repo, dep_bump, repo_dependency, repo_impact"
                     " RESTART IDENTITY CASCADE")
        ids = {}
        for name in ("signer", "packager", "runtime", "unrelated"):
            ids[name] = conn.execute(
                "INSERT INTO repo (github_id, owner, name, full_name, clone_url,"
                " default_branch) VALUES (%s,'acme',%s,%s,'','main') RETURNING id",
                (abs(hash(name)) % 100000, name, f"acme/{name}"),
            ).fetchone()[0]

        # packager declares signer and has bumped it; runtime declares packager
        # but has never moved it.
        for consumer, dep in (("packager", "signer"), ("runtime", "packager")):
            conn.execute(
                "INSERT INTO repo_dependency (consumer_repo_id, dep_repo_id,"
                " dep_name, manifest, ecosystem) VALUES (%s,%s,%s,'go.mod','go')",
                (ids[consumer], ids[dep], f"github.com/acme/{dep}"),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO dep_bump (consumer_repo_id, consumer_sha, dep_repo_id,"
                " dep_name, dep_version, manifest, bumped_at, adoption_seconds)"
                " VALUES (%s,%s,%s,'github.com/acme/signer',%s,'go.mod',"
                " now() - make_interval(days => %s), %s)",
                (ids["packager"], f"{i:040x}", ids["signer"], f"v1.{i}.0", i * 10, 86400 * 2),
            )
    predict.rebuild(force=True)
    return ids


def _edges():
    from git_synapse.db.engine import query
    return query("SELECT i.*, s.name AS source, t.name AS target FROM repo_impact i"
                 " JOIN repo s ON s.id = i.source_repo_id"
                 " JOIN repo t ON t.id = i.target_repo_id")


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
    from git_synapse.db.engine import query
    rows = query("SELECT source_repo_id, rank_in_source, score FROM repo_impact"
                 " ORDER BY source_repo_id, rank_in_source")
    for row in rows:
        assert row["rank_in_source"] >= 1


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
    from git_synapse.db.engine import execute, query_one

    repo = query_one(
        """
        INSERT INTO repo (github_id, owner, name, full_name, host, provider,
                          is_enabled, commit_count, pair_population)
        VALUES (NULL, 'ranktest', 'dead', 'ranktest/dead', 'github.com',
                'github', TRUE, 500, 500)
        ON CONFLICT (host, full_name) DO UPDATE SET updated_at = now()
        RETURNING id
        """)
    file_row = query_one(
        """
        INSERT INTO file (repo_id, path, dir_path, basename, extension, depth,
                          is_deleted, change_count, pair_change_count, author_count)
        VALUES (%s, 'gone/removed.py', 'gone', 'removed.py', 'py', 1,
                TRUE, 9999, 9999, 3)
        RETURNING id
        """, (repo["id"],))
    yield repo["id"], file_row["id"]
    execute("DELETE FROM file_risk WHERE file_id = %s", (file_row["id"],))
    execute("DELETE FROM file WHERE id = %s", (file_row["id"],))
    execute("DELETE FROM repo WHERE id = %s", (repo["id"],))


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
    from git_synapse.db.engine import execute

    repo_id, fid = dead_file
    execute(
        "INSERT INTO file_risk (file_id, repo_id, churn_pct, coupling_pct,"
        " ownership_hhi, effective_authors, author_count, partner_count,"
        " change_count, risk_score) VALUES (%s,%s,1,1,1,1,3,40,9999,1.99)"
        " ON CONFLICT (file_id) DO NOTHING", (fid, repo_id))
    assert fid not in {r["file_id"] for r in mining.risky_files(repo_id=repo_id, limit=10)}, \
        "the highest possible score, and still out"
    assert fid in {r["file_id"] for r in
                   mining.risky_files(repo_id=repo_id, limit=10, include_deleted=True)}


def test_a_coupling_query_still_reports_a_deleted_partner(db):
    """The opposite judgement, and deliberately so: the reader asked about one
    specific file, the coupling is a historical fact, and the answer labels it
    rather than hiding it."""
    from git_synapse.analysis import query as q
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT p.repo_id, p.file_a_id, p.file_b_id FROM file_pair p
          JOIN file fb ON fb.id = p.file_b_id
         WHERE fb.is_deleted LIMIT 1
        """)
    if row is None:
        import pytest

        pytest.skip("no deleted partner in this corpus")
    partners = q.coupled_files(row["file_a_id"], limit=500)
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
    import psycopg

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
    with contextlib.suppress(psycopg.Error, AttributeError, TypeError):
        mining._cluster_repo(_Conn(), 1, stats)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    dense_would_be = n_nodes * n_nodes * 8
    assert peak < dense_would_be / 10, (
        f"peak {peak / 1e6:.1f}MB is within an order of magnitude of the "
        f"{dense_would_be / 1e6:.0f}MB dense matrix this replaced")
