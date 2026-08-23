"""The MCP tools, called the way an agent calls them.

These are the only surface an agent sees. A wrong shape here does not raise --
it becomes a confident answer, which is the expensive kind of wrong.
"""
from __future__ import annotations

import pytest

from git_synapse.mcp import server


# ------------------------------------------------------------- resolution

@pytest.mark.parametrize("name", ["", "   ", "definitely-not-a-repo", "hubbl", "api"])
def test_a_name_that_is_not_a_repository_is_refused(db, name):
    """A substring match resolved to the first hit, so a typo answered
    confidently about a different repository."""
    out = server.upstream_repos(repo=name)
    assert "error" in out, f"{name!r} resolved to {out.get('repo')}"


def test_real_names_full_names_and_case_all_resolve(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name, full_name FROM repo WHERE is_enabled LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    for form in (row["name"], row["full_name"], row["name"].upper()):
        out = server.upstream_repos(repo=form)
        assert "error" not in out, f"{form!r} failed to resolve"
        assert out["repo"] == row["full_name"]


# ---------------------------------------------------------- coupled_files

def test_coupled_files_rejects_an_unknown_path_with_a_hint(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    out = server.coupled_files(repo=row["name"], path="no/such/file.go")
    assert "error" in out and "hint" in out


def test_coupled_files_shape_is_stable(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, f.path FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE f.change_count > 30 AND NOT f.is_deleted LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no busy file")
    out = server.coupled_files(repo=row["repo"], path=row["path"], min_support=3, limit=5)
    assert "error" not in out, out
    assert out["file"]["path"] == row["path"]
    for p in out["partners"]:
        for key in ("path", "score", "co_changes", "partner_total_changes",
                    "probability_also_changes", "informative", "currency"):
            assert key in p, f"{key} missing from a partner row"
        assert p["partner_total_changes"] >= p["co_changes"]
        assert 0.0 <= p["probability_also_changes"] <= 1.0


def test_an_unknown_measure_is_refused(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        "SELECT r.name AS repo, f.path FROM file f JOIN repo r ON r.id=f.repo_id LIMIT 1"
    )
    out = server.coupled_files(repo=row["repo"], path=row["path"], measure="nope")
    assert "error" in out


# ----------------------------------------------------------------- currency

@pytest.mark.parametrize(
    ("days", "trend", "deleted", "starts"),
    [
        (3, None, True, "DELETED"),        # deletion outranks everything
        (3, "emerging", True, "DELETED"),
        (400, "emerging", False, "STALE"), # age outranks trend
        (400, "decaying", False, "STALE"),
        (400, None, False, "STALE"),
        (100, "decaying", False, "DECAYING"),
        (12, "emerging", False, "emerging"),
        (5, None, False, "current"),
        (60, None, False, "co-changed"),
    ],
)
def test_currency_precedence(days, trend, deleted, starts):
    assert server._describe_currency(days, trend, deleted).startswith(starts)


def test_currency_is_absent_when_recency_is_unknown():
    assert server._describe_currency(None, None, False) is None


# ------------------------------------------------------------- module tools

def test_module_context_rejects_a_path_that_does_not_exist(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    out = server.module_context(repo=row["name"], path="not/real.go")
    assert "error" in out


def test_coupled_directories_accepts_a_file_or_a_directory(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, f.path, f.dir_path
        FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE f.dir_path <> '' AND f.change_count > 20 LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no suitable file")
    by_file = server.coupled_directories(repo=row["repo"], path=row["path"], limit=5)
    by_dir = server.coupled_directories(repo=row["repo"], path=row["dir_path"], limit=5)
    assert by_file["directory"]["path"] == by_dir["directory"]["path"] == row["dir_path"]


# --------------------------------------------------------------- catalogue

def test_list_measures_describes_every_measure(db):
    out = server.list_measures()
    measures = out["measures"] if isinstance(out, dict) else out
    assert len(measures) >= 29
    assert all(m.get("key") and m.get("label") for m in measures)


def test_list_repositories_returns_usable_names(db):
    out = server.list_repositories(limit=5)
    repos = out["repositories"] if isinstance(out, dict) else out
    assert repos
    for r in repos:
        assert r.get("name")


def test_search_files_finds_a_known_path(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT basename FROM file WHERE basename = 'go.mod' LIMIT 1")
    if row is None:
        pytest.skip("no go.mod indexed")
    out = server.search_files(term="go.mod", limit=5)
    files = out["files"] if isinstance(out, dict) else out
    assert files


def test_report_gap_rejects_an_opinion_and_accepts_a_defect(db):
    from git_synapse.db.engine import connection

    bad = server.report_gap(kind="opinion", detail="I disagree with the ranking")
    assert "error" in bad

    good = server.report_gap(
        kind="wrong_data", detail="PROBE mcp tool test", severity="low",
        tool="coupled_files", repo="t/probe", expected="x", observed="y",
    )
    assert "error" not in good and good.get("id")
    with connection() as conn:
        conn.execute("DELETE FROM feedback WHERE id=%s", (good["id"],))
