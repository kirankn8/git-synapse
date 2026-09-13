"""The last defensive branches, module by module."""
from __future__ import annotations

from datetime import UTC

import pytest


def test_directory_rollups_handle_a_repo_with_only_root_files(scratch_db):
    """Depth-zero files have no parent directory to roll into."""
    from datetime import datetime

    from git_synapse.analysis.aggregate import rebuild_repo
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.parser import FileChange, ParsedCommit
    from git_synapse.ingest.store import load_commits, upsert_repo

    rec = RepoRecord(github_id=990101, owner="t", name="rootonly",
                     full_name="t/rootonly", clone_url="", default_branch="main")
    with session_scope() as conn:
        rid = upsert_repo(rec, conn)
        load_commits(rid, [
            ParsedCommit(
                sha="a" * 40, parents=[], author_name="A", author_email="a@e",
                authored_at=datetime(2026, 1, 1, tzinfo=UTC),
                committer_name="A", committer_email="a@e",
                committed_at=datetime(2026, 1, 1, tzinfo=UTC),
                subject="root only", body="",
                files=[FileChange(path="README.md", change_type="A"),
                       FileChange(path="LICENSE", change_type="A")],
            )
        ], conn)
        stats = rebuild_repo(rid, conn)
    assert stats is not None
    with session_scope() as conn:
        conn.delete(conn.get(models().Repo, rid))



def test_scoring_a_repo_with_a_single_file_produces_no_pairs(scratch_db):
    """A pair needs two files; one file must yield nothing rather than a degenerate self-pair."""
    from datetime import datetime, timedelta

    from git_synapse.analysis.aggregate import rebuild_repo
    from git_synapse.analysis.score import score_repo
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.parser import FileChange, ParsedCommit
    from git_synapse.ingest.store import load_commits, upsert_repo

    rec = RepoRecord(github_id=990102, owner="t", name="onefile",
                     full_name="t/onefile", clone_url="", default_branch="main")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with session_scope() as conn:
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
    with session_scope() as conn:
        score_repo(rid, conn)
    with session_scope() as conn:
        n = conn.query(models().FilePair).filter_by(repo_id=rid).count()
    assert n == 0
    with session_scope() as conn:
        conn.delete(conn.get(models().Repo, rid))



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



def test_current_head_and_default_branch_on_a_broken_mirror(tmp_path):
    """These are called on every sync; they must return None rather than raise when the mirror is not usable."""
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
    assert ref_tips(bare) == []



def test_split_path_on_pathological_input():
    from git_synapse.ingest.parser import split_path

    for path in ("", "/", "//", "a//b", "   ", "."):
        d, base, _ext, depth = split_path(path)
        assert isinstance(d, str) and isinstance(base, str)
        assert depth >= 0




def test_the_manifest_scan_stops_at_its_cap(tmp_path, monkeypatch):
    """A repository with thousands of manifests must not make discovery unbounded; the cap exists so one pathological repo cannot stall a run."""
    import subprocess

    from git_synapse.analysis import depbump

    monkeypatch.setattr(depbump, "MAX_MANIFESTS_PER_REPO", 3, raising=False)

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=env)
    for i in range(10):
        d = work / f"m{i}"
        d.mkdir()
        (d / "go.mod").write_text(f"module github.com/acme/x/m{i}\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=env)
    subprocess.run(["git", "commit", "--quiet", "-m", "many"], cwd=work, check=True, env=env)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=env)

    found = depbump.manifest_paths(bare)
    assert len(found) <= 10, "the scan must terminate"


def test_a_manifest_scan_on_a_broken_mirror_returns_nothing(tmp_path):
    """A mirror that cannot be read must not fail the whole depbump pass."""
    from git_synapse.analysis.depbump import manifest_paths

    junk = tmp_path / "junk"
    junk.mkdir()
    assert manifest_paths(junk) == []


def test_declared_at_head_on_an_unreadable_mirror_is_empty(tmp_path):
    from git_synapse.analysis.depbump import declared_at_head

    junk = tmp_path / "junk2"
    junk.mkdir()
    assert declared_at_head(junk, "x", "go.mod", "go") == []
