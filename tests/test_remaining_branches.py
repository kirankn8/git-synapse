"""The last defensive branches, module by module.

Nothing exotic here: guards for empty inputs, absent rows and unusual shapes.
They exist because someone anticipated the case, and until they are exercised
that anticipation is untested.
"""
from __future__ import annotations

import pytest


# ------------------------------------------------------------- aggregate

def test_directory_rollups_handle_a_repo_with_only_root_files(scratch_db):
    """Depth-zero files have no parent directory to roll into."""
    from git_synapse.analysis.aggregate import rebuild_repo
    from git_synapse.db.engine import connection
    from git_synapse.ingest.parser import FileChange, ParsedCommit
    from git_synapse.ingest.store import load_commits, upsert_repo
    from git_synapse.ingest.github import RepoRecord
    from datetime import datetime, timezone

    rec = RepoRecord(github_id=990101, owner="t", name="rootonly",
                     full_name="t/rootonly", clone_url="", default_branch="main")
    with connection() as conn:
        rid = upsert_repo(rec, conn)
        load_commits(rid, [
            ParsedCommit(
                sha="a" * 40, parents=[], author_name="A", author_email="a@e",
                authored_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                committer_name="A", committer_email="a@e",
                committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                subject="root only", body="",
                files=[FileChange(path="README.md", change_type="A"),
                       FileChange(path="LICENSE", change_type="A")],
            )
        ], conn)
        stats = rebuild_repo(rid, conn)
    assert stats is not None
    with connection() as conn:
        conn.execute("DELETE FROM repo WHERE id=%s", (rid,))


# ----------------------------------------------------------------- score

def test_scoring_a_repo_with_a_single_file_produces_no_pairs(scratch_db):
    """A pair needs two files; one file must yield nothing rather than a
    degenerate self-pair."""
    from git_synapse.analysis.aggregate import rebuild_repo
    from git_synapse.analysis.score import score_repo
    from git_synapse.db.engine import connection, query_one
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.parser import FileChange, ParsedCommit
    from git_synapse.ingest.store import load_commits, upsert_repo
    from datetime import datetime, timedelta, timezone

    rec = RepoRecord(github_id=990102, owner="t", name="onefile",
                     full_name="t/onefile", clone_url="", default_branch="main")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with connection() as conn:
        rid = upsert_repo(rec, conn)
        load_commits(rid, [
            ParsedCommit(
                sha=f"{i:040x}", parents=[], author_name="A", author_email="a@e",
                authored_at=base + timedelta(hours=i), committer_name="A",
                committer_email="a@e", committed_at=base + timedelta(hours=i),
                subject=f"c{i}", body="",
                files=[FileChange(path="only.py", change_type="M")],
            ) for i in range(4)
        ], conn)
        rebuild_repo(rid, conn)
    with connection() as conn:
        score_repo(rid, conn)
    n = query_one("SELECT count(*) AS n FROM file_pair WHERE repo_id=%s", (rid,))["n"]
    assert n == 0
    with connection() as conn:
        conn.execute("DELETE FROM repo WHERE id=%s", (rid,))


# ----------------------------------------------------------------- query

def test_limits_are_clamped_rather_than_trusted(db):
    from git_synapse.analysis.query import _clamp_limit
    from git_synapse.config import get_config

    cap = get_config().analysis.max_limit
    assert _clamp_limit(0) >= 1
    assert _clamp_limit(-5) >= 1
    assert _clamp_limit(None) >= 1
    assert _clamp_limit(10**9) <= cap
    assert _clamp_limit(5) == 5


def test_an_unknown_measure_names_the_valid_ones(db):
    """The error has to be actionable, or the caller guesses again."""
    from git_synapse.analysis.query import _safe_order

    with pytest.raises(KeyError) as exc:
        _safe_order("definitely_not_a_measure")
    assert "npmi" in str(exc.value)


def test_measure_aliases_resolve(db):
    from git_synapse.analysis.query import _safe_order

    assert _safe_order("cosine") == "ochiai"
    assert _safe_order("lift") == "association_strength"
    assert _safe_order("") == _safe_order(None)


# ---------------------------------------------------------------- gitops

def test_current_head_and_default_branch_on_a_broken_mirror(tmp_path):
    """These are called on every sync; they must return None rather than raise
    when the mirror is not usable."""
    from git_synapse.ingest.gitops import current_head, default_branch, repo_size_kb

    junk = tmp_path / "junk"
    junk.mkdir()
    assert current_head(junk) is None
    assert default_branch(junk) is None
    assert repo_size_kb(junk) >= 0


def test_ref_tips_on_a_repository_with_no_commits(tmp_path):
    import subprocess

    from git_synapse.ingest.gitops import ref_tips

    bare = tmp_path / "empty.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(bare)], check=True)
    # git exits 0 and echoes the literal "HEAD" here; storing that as a
    # watermark would make the next run exclude everything.
    assert ref_tips(bare) == []


# ---------------------------------------------------------------- parser

def test_split_path_on_pathological_input():
    from git_synapse.ingest.parser import split_path

    for path in ("", "/", "//", "a//b", "   ", "."):
        d, base, ext, depth = split_path(path)
        assert isinstance(d, str) and isinstance(base, str)
        assert depth >= 0


# ----------------------------------------------------- reporting with rows

def test_ingest_reports_the_repositories_that_failed(db, monkeypatch):
    """A run that half-worked must name what did not, or the operator has to
    read logs to find out."""
    from typer.testing import CliRunner

    from git_synapse.cli import app
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import RepoResult

    class _Run:
        status, run_id, duration_s, commits_added = "partial", 7, 1.0, 0
        ok = [RepoResult(full_name="t/good", status="ok")]
        failed = [RepoResult(full_name="t/bad", status="failed", error="boom")]
        repos = ok + failed

    monkeypatch.setattr(pipeline, "load_repo_records", lambda: [
        RepoRecord(github_id=1, owner="t", name="good", full_name="t/good",
                   clone_url="", default_branch="main")
    ])
    monkeypatch.setattr(pipeline, "run_ingest", lambda **kw: _Run())

    r = CliRunner().invoke(app, ["ingest"])
    assert r.exit_code == 0, r.stdout
    assert "t/bad" in r.stdout, "a failed repository must be named"


def test_score_reports_for_every_repo_when_asked(db, monkeypatch):
    from typer.testing import CliRunner

    from git_synapse.analysis import score
    from git_synapse.cli import app

    monkeypatch.setattr(score, "score_all", lambda: 3, raising=False)
    r = CliRunner().invoke(app, ["score", "--repo-id", "0"])
    assert r.exit_code == 0, r.stdout


def test_depbump_prints_propagation_lags_when_there_are_any(db):
    from typer.testing import CliRunner

    from git_synapse.analysis.depbump import propagation_lags
    from git_synapse.cli import app

    if not propagation_lags(limit=1):
        pytest.skip("no propagation lags recorded")
    r = CliRunner().invoke(app, ["depbump"])
    assert r.exit_code == 0, r.stdout
    assert "lag" in r.stdout.lower()


def test_impact_prints_rows_for_a_repo_that_has_them(db):
    from typer.testing import CliRunner

    from git_synapse.analysis.query import query_one
    from git_synapse.cli import app

    row = query_one(
        """
        SELECT r.name FROM repo_impact i JOIN repo r ON r.id = i.source_repo_id
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("impact table empty")
    r = CliRunner().invoke(app, ["impact", row["name"]])
    assert r.exit_code == 0, r.stdout


# ------------------------------------------------------------ copy helpers

def test_copy_into_temp_creates_and_loads_in_one_step(db):
    from git_synapse.db.engine import connection, copy_into_temp

    with connection() as conn:
        n = copy_into_temp(
            conn, "probe_tmp", [("a", "int"), ("b", "text")],
            [(1, "x"), (2, "y")],
        )
        assert n == 2
        assert conn.execute("SELECT count(*) FROM probe_tmp").fetchone()[0] == 2


def test_copy_into_temp_with_no_rows(db):
    from git_synapse.db.engine import connection, copy_into_temp

    with connection() as conn:
        assert copy_into_temp(conn, "probe_tmp_empty", [("a", "int")], []) == 0
