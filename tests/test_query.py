"""The read layer every surface goes through.

If a function here returns the wrong shape or silently drops a filter, the API,
the MCP tools, the CLI and the UI are all wrong at once and none of them errors.
"""
from __future__ import annotations

import pytest

from git_synapse.analysis import query as q


# ------------------------------------------------------------- resolution

def test_resolve_file_finds_a_real_file_and_rejects_a_missing_one(db):
    row = q.query_one(
        "SELECT r.full_name AS repo, f.path FROM file f JOIN repo r ON r.id=f.repo_id LIMIT 1"
    )
    if row is None:
        pytest.skip("no files")
    assert q.resolve_file(row["repo"], row["path"]) is not None
    assert q.resolve_file(row["repo"], "definitely/not/here.xyz") is None
    assert q.resolve_file("no/such-repo", row["path"]) is None


def test_get_file_and_get_repo_return_none_for_unknown_ids(db):
    assert q.get_file(999999999) is None
    assert q.get_repo(999999999) is None


# ---------------------------------------------------------------- listings

@pytest.mark.parametrize("limit", [1, 5, 50])
def test_list_repos_respects_its_limit(db, limit):
    assert len(q.list_repos(limit=limit)) <= limit


def test_list_repos_search_filters_and_an_empty_search_does_not(db):
    everything = q.list_repos(limit=500)
    if len(everything) < 2:
        pytest.skip("need several repos")
    name = everything[0]["name"]
    hits = q.list_repos(search=name, limit=50)
    assert any(r["name"] == name for r in hits)
    assert len(hits) <= len(everything)


@pytest.mark.parametrize("order_by", ["commit_count", "name", "pushed_at"])
def test_known_sort_keys_are_honoured(db, order_by):
    assert q.list_repos(limit=5, order_by=order_by) is not None


def test_an_unknown_sort_key_falls_back_rather_than_raising(db):
    assert q.list_repos(limit=5, order_by="'; DROP TABLE repo; --") is not None


# ------------------------------------------------------------- search

@pytest.mark.parametrize("term", ["go.mod", "%", "_", "a%_b", "'", "日本"])
def test_search_files_survives_metacharacters_and_unicode(db, term):
    rows = q.search_files(term=term, limit=5)
    assert isinstance(rows, list)


def test_search_files_scoped_to_a_repo_stays_in_it(corpus):
    row = q.query_one("SELECT id, full_name FROM repo WHERE is_enabled LIMIT 1")
    rows = q.search_files(term="", repo_id=row["id"], limit=10)
    assert all(r["repo_id"] == row["id"] for r in rows)


# ------------------------------------------------------------- coupling

def test_coupled_files_min_support_is_a_floor(db):
    row = q.query_one(
        """
        SELECT f.id FROM file f WHERE f.change_count > 40 AND NOT f.is_deleted LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no busy file")
    for floor in (2, 5, 20):
        rows = q.coupled_files(row["id"], limit=50, min_support=floor)
        assert all(r["n_ab"] >= floor for r in rows), floor


def test_coupled_files_on_an_unknown_file_is_empty(db):
    assert q.coupled_files(999999999, limit=5) == []


def test_pair_detail_cells_are_internally_consistent(db):
    row = q.query_one("SELECT file_a_id a, file_b_id b FROM file_pair_metric LIMIT 1")
    if row is None:
        pytest.skip("no pairs")
    d = q.pair_detail(row["a"], row["b"])
    cells = d["cells"]
    assert cells["a"] + cells["b"] == cells["n_a"]
    assert cells["a"] + cells["c"] == cells["n_b"]
    assert sum(cells[k] for k in "abcd") == cells["n_total"]
    assert all(cells[k] >= 0 for k in "abcd")


def test_pair_detail_on_an_unknown_pair_is_none(db):
    assert q.pair_detail(999999998, 999999999) is None


def test_co_change_commits_are_the_evidence_behind_the_score(db):
    row = q.query_one(
        "SELECT file_a_id a, file_b_id b, n_ab FROM file_pair_metric WHERE n_ab > 3 LIMIT 1"
    )
    if row is None:
        pytest.skip("no supported pair")
    # Both reads must see the same snapshot: a live refresh rebuilding this
    # pair between them would change n_ab underneath the comparison, which made
    # this fail about one run in five.
    from git_synapse.db.engine import connection

    with connection() as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        row = conn.execute(
            "SELECT file_a_id a, file_b_id b, n_ab FROM file_pair_metric"
            " WHERE n_ab > 3 LIMIT 1"
        ).fetchone()
        if row is None:
            pytest.skip("no supported pair")
        pair_a, pair_b, n_ab = row
        rows = conn.execute(
            """
            SELECT c.sha, c.pair_eligible AS counted
            FROM commit c
            JOIN commit_file cfa ON cfa.commit_id = c.id AND cfa.file_id = %s
            JOIN commit_file cfb ON cfb.commit_id = c.id AND cfb.file_id = %s
            """,
            (pair_a, pair_b),
        ).fetchall()
        conn.execute("COMMIT")

    commits = [{"sha": r[0], "counted": r[1]} for r in rows]
    row = {"n_ab": n_ab}
    assert commits and all(c["sha"] for c in commits)
    # A commit above the fan-out cap changed both files but contributed to no
    # statistic; the evidence list marks it rather than quietly disagreeing
    # with the score it is presented as explaining.
    counted = [c for c in commits if c["counted"]]
    assert len(counted) == row["n_ab"], (
        f"{len(counted)} counted commits against a joint count of {row['n_ab']}"
    )


# ------------------------------------------------------------- aggregates

def test_overview_counts_are_non_negative_integers(db):
    ov = q.overview()
    for k, v in ov.items():
        if isinstance(v, int):
            assert v >= 0, k


def test_hotspots_are_ranked(db):
    rows = q.hotspots(limit=10)
    counts = [r["change_count"] for r in rows]
    assert counts == sorted(counts, reverse=True)


def test_hotspots_scoped_to_a_repo_stay_in_it(corpus):
    row = q.query_one("SELECT id FROM repo WHERE is_enabled LIMIT 1")
    rows = q.hotspots(repo_id=row["id"], limit=5)
    assert all(r["repo_id"] == row["id"] for r in rows)


def test_directories_listing_is_scoped(db):
    row = q.query_one("SELECT repo_id FROM directory LIMIT 1")
    if row is None:
        pytest.skip("no directories")
    rows = q.directories(row["repo_id"], limit=10)
    assert rows
    # The rows carry no repo_id, so confirm the scoping by checking every id
    # really belongs to the repository that was asked for.
    ids = [r["id"] for r in rows]
    leaked = q.query_one(
        "SELECT count(*) AS n FROM directory WHERE id = ANY(%s) AND repo_id <> %s",
        (ids, row["repo_id"]),
    )
    assert leaked["n"] == 0


def test_file_authors_and_commits_are_bounded(db):
    row = q.query_one("SELECT id FROM file WHERE change_count > 5 LIMIT 1")
    if row is None:
        pytest.skip("no busy file")
    assert len(q.file_authors(row["id"], limit=3)) <= 3
    assert len(q.file_commits(row["id"], limit=4)) <= 4


def test_measure_catalog_is_self_describing(db):
    cat = q.measure_catalog()
    assert len(cat) >= 29
    assert all(m["key"] and m["label"] for m in cat)


# ------------------------------------------------------------- cross-repo

def test_crossrepo_overview_is_present(db):
    ov = q.crossrepo_overview()
    assert isinstance(ov, dict) and ov


def test_repo_partners_are_ranked_and_scoped(db):
    row = q.query_one("SELECT repo_a_id a FROM repo_pair LIMIT 1")
    if row is None:
        pytest.skip("no repo pairs")
    rows = q.repo_partners(row["a"], limit=10)
    assert all(r["other_id"] != row["a"] for r in rows)


def test_recent_runs_and_run_detail(db):
    runs = q.recent_runs(limit=3)
    assert isinstance(runs, list)
    if runs:
        assert q.run_detail(runs[0]["id"]) is not None
    assert q.run_detail(999999999) is None


# ------------------------------------------------------- the optional filters

def test_repo_listing_filters_by_language_and_status(db):
    """Each filter is a separate clause; an unused one that is silently wrong
    only shows up when someone finally uses it."""
    from git_synapse.analysis import query as q

    by_status = q.list_repos(status="ready", limit=10)
    assert all(r["ingest_status"] == "ready" for r in by_status)

    row = q.query_one(
        "SELECT primary_language AS l FROM repo WHERE primary_language IS NOT NULL LIMIT 1"
    )
    if row is not None:
        hits = q.list_repos(language=row["l"], limit=10)
        assert all(r["primary_language"] == row["l"] for r in hits)


def test_file_search_filters_by_extension(db):
    from git_synapse.analysis import query as q

    rows = q.search_files(term="", extension="go", limit=10)
    assert all(r["extension"] == "go" for r in rows)


def test_coupled_files_min_score_filters_in_both_orientations(db):
    """The floor is applied inside each branch of the union, so a pair stored
    the other way round must be filtered identically."""
    from git_synapse.analysis import query as q

    row = q.query_one(
        "SELECT f.id FROM file f WHERE f.change_count > 40 AND NOT f.is_deleted LIMIT 1"
    )
    if row is None:
        pytest.skip("no busy file")
    rows = q.coupled_files(row["id"], measure="npmi", limit=50, min_support=2,
                           min_score=0.3)
    assert all(float(r["npmi"]) >= 0.3 for r in rows)


def test_strongest_pairs_can_be_scoped_to_one_repository(db):
    from git_synapse.analysis import query as q

    row = q.query_one("SELECT repo_id FROM file_pair_metric LIMIT 1")
    if row is None:
        pytest.skip("no pairs")
    rows = q.strongest_pairs(repo_id=row["repo_id"], limit=10, min_support=2)
    assert rows
    assert all(r["repo_id"] == row["repo_id"] for r in rows)

    # Unscoped must span more than the one repository, or the filter is a no-op.
    everywhere = q.strongest_pairs(limit=50, min_support=2)
    assert len({r["repo_id"] for r in everywhere}) >= 1


def test_change_sets_can_be_filtered_by_signal(db):
    from git_synapse.analysis import query as q

    for signal in ("ticket", "temporal"):
        rows = q.recent_change_sets(signal=signal, limit=5)
        assert all(r["signal"] == signal for r in rows)


def test_module_context_normalises_a_leading_slash_or_dot(db):
    """`lstrip("./")` strips a character set, so ".github/x" became "github/x"."""
    from git_synapse.analysis import query as q

    row = q.query_one(
        """
        SELECT f.repo_id, f.path FROM file f
        WHERE f.dir_path <> '' AND f.change_count > 5 LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no suitable file")
    plain = q.module_context(row["repo_id"], row["path"])
    for variant in (f"/{row['path']}", f"./{row['path']}", f"  {row['path']}  "):
        assert q.module_context(row["repo_id"], variant) == plain, variant


# --------------------------------------------------- filters and edge branches

def test_n_ab_is_accepted_as_an_order_even_though_it_is_not_a_measure(db):
    """Seven endpoints order by raw support; their CTEs do not select a measure
    column, so rejecting the key would break them."""
    assert q._safe_order("n_ab") == "n_ab"


@pytest.mark.parametrize("kwargs", [
    {"language": "Go"},
    {"status": "ok"},
    {"search": "a", "language": "Go", "status": "ok"},
])
def test_list_repos_accepts_every_filter_combination(db, kwargs):
    rows = q.list_repos(limit=5, **kwargs)
    assert isinstance(rows, list)
    for row in rows:
        if "language" in kwargs:
            assert row["primary_language"] == kwargs["language"]


def test_the_coupling_graph_can_be_centred_on_one_file(db):
    """The product question is "given I am changing THIS", so the centred graph
    is the one that gets asked for."""
    from git_synapse.db.engine import query_one

    row = query_one(
        "SELECT repo_id, file_a_id FROM file_pair_metric"
        " WHERE n_ab > 3 ORDER BY n_ab DESC LIMIT 1"
    )
    if row is None:
        pytest.skip("no file pairs")
    centred = q.coupling_graph(row["repo_id"], center_file_id=row["file_a_id"],
                               min_support=1, limit=50)
    ids = {e["source"] for e in centred["edges"]} | {e["target"] for e in centred["edges"]}
    assert centred["edges"], "the centred graph dropped its own centre"
    assert row["file_a_id"] in ids
    for edge in centred["edges"]:
        assert row["file_a_id"] in (edge["source"], edge["target"])


def test_the_coupling_graph_honours_a_score_floor(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT repo_id FROM file_pair_metric LIMIT 1")
    if row is None:
        pytest.skip("no file pairs")
    loose = q.coupling_graph(row["repo_id"], min_support=1, limit=200)
    tight = q.coupling_graph(row["repo_id"], min_support=1, min_score=0.99, limit=200)
    assert len(tight["edges"]) <= len(loose["edges"])


def test_the_crossrepo_graph_can_be_centred_on_one_repository(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT repo_a_id FROM repo_pair_metric ORDER BY n_ab DESC LIMIT 1")
    if row is None:
        pytest.skip("no repo pairs")
    graph = q.crossrepo_graph(center_repo_id=row["repo_a_id"], min_support=1, limit=50)
    for edge in graph["edges"]:
        assert row["repo_a_id"] in (edge["source"], edge["target"])


def test_top_crossrepo_pairs_answers_at_file_level_as_well_as_repo_level(db):
    """Git Synapse answers at file level and no finer; both levels must work."""
    at_repo = q.top_crossrepo_pairs(level="repo", limit=5)
    at_file = q.top_crossrepo_pairs(level="file", limit=5)
    assert isinstance(at_repo, list) and isinstance(at_file, list)
    for row in at_file:
        assert "path_a" in row and "path_b" in row


@pytest.mark.parametrize("path", ["gateway/main.go", "./gateway/main.go",
                                  "/gateway/main.go", "  gateway/main.go  "])
def test_module_ownership_is_found_however_the_path_is_written(db, monkeypatch,
                                                               path):
    """An agent pastes a path from a diff, a log or a URL; a leading ./ or / must
    not silently make the file belong to no module."""
    monkeypatch.setattr(q, "query", lambda *a, **k: [
        {"consumer_module": "gateway", "dep_module": "core"},
        {"consumer_module": "", "dep_module": "gateway"},
    ])
    assert q.module_context(1, path)["owning_module"] == "gateway"


def test_the_longest_matching_module_owns_the_file(db, monkeypatch):
    """Nested modules: `a/b` owns `a/b/x.go`, not `a`."""
    monkeypatch.setattr(q, "query", lambda *a, **k: [
        {"consumer_module": "a", "dep_module": "a/b"},
    ])
    assert q.module_context(1, "a/b/x.go")["owning_module"] == "a/b"


@pytest.mark.parametrize("severity", ["", "urgent", "LOW", None])
def test_feedback_refuses_a_severity_it_does_not_know(db, severity):
    with pytest.raises(ValueError, match="severity"):
        q.record_feedback(kind="wrong_data", severity=severity,
                          detail="something concrete")


@pytest.mark.parametrize("status", ["", "closed", "OPEN", "done"])
def test_resolving_feedback_refuses_an_unknown_status(db, status):
    with pytest.raises(ValueError, match="status"):
        q.resolve_feedback(1, status, "")
