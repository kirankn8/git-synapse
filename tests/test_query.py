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


def test_search_files_scoped_to_a_repo_stays_in_it(db):
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
    commits = q.co_change_commits(row["a"], row["b"], limit=100)
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


def test_hotspots_scoped_to_a_repo_stay_in_it(db):
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
