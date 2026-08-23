"""The remaining guards, grouped by what they protect against.

Everything here is a branch someone wrote deliberately: a retry, a fallback, an
empty-input case. They are the cheapest things in the codebase to get subtly
wrong and among the most annoying to debug, because they only run when something
else has already gone wrong.
"""
from __future__ import annotations

import subprocess

import pytest

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


# ------------------------------------------------------------ network retry

def test_a_network_git_command_retries_then_gives_up(tmp_path, monkeypatch):
    """Four attempts at a two-minute timeout is nine minutes per repository;
    the retry has to happen, and it has to end."""
    from git_synapse.ingest import gitops
    from git_synapse.ingest.gitops import GitError, run_git_network

    monkeypatch.setattr(gitops.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_transient(args, cwd=None, timeout=None, check=True):
        calls["n"] += 1
        raise GitError(args, 128, "fatal: unable to access: Failed to connect")

    monkeypatch.setattr(gitops, "run_git", always_transient)
    with pytest.raises(GitError):
        run_git_network(["fetch"], cwd=tmp_path)
    assert calls["n"] == gitops.NETWORK_RETRIES


def test_a_permanent_git_failure_is_not_retried(tmp_path, monkeypatch):
    from git_synapse.ingest import gitops
    from git_synapse.ingest.gitops import GitError, run_git_network

    monkeypatch.setattr(gitops.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def auth_failure(args, cwd=None, timeout=None, check=True):
        calls["n"] += 1
        raise GitError(args, 128, "remote: Invalid username or token.")

    monkeypatch.setattr(gitops, "run_git", auth_failure)
    with pytest.raises(GitError):
        run_git_network(["fetch"], cwd=tmp_path)
    assert calls["n"] == 1, "a credential failure must not be retried"


def test_a_git_timeout_is_reported_as_one(tmp_path, monkeypatch):
    from git_synapse.ingest import gitops
    from git_synapse.ingest.gitops import GitError, run_git_network

    monkeypatch.setattr(gitops.time, "sleep", lambda _s: None)

    def slow(args, cwd=None, timeout=None, check=True):
        raise subprocess.TimeoutExpired(args, timeout or 1)

    monkeypatch.setattr(gitops, "run_git", slow)
    with pytest.raises(GitError) as exc:
        run_git_network(["fetch"], cwd=tmp_path)
    assert "timed out" in str(exc.value).lower()


# ------------------------------------------------------------- blobless mode

def test_a_blobless_mirror_is_detected_and_has_no_line_counts(tmp_path):
    """Blobless clones cannot count lines; claiming otherwise would make churn
    statistics silently wrong for those repositories."""
    from git_synapse.ingest.gitops import clone_mirror, mirror_is_blobless
    from git_synapse.ingest.parser import iter_commits

    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    (work / "a.txt").write_text("one\ntwo\nthree\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "c"], cwd=work, check=True, env=ENV)
    src = tmp_path / "src.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(src)],
                   check=True, env=ENV)

    dest = tmp_path / "blobless.git"
    clone_mirror(str(src), dest, blobless=True)
    assert mirror_is_blobless(dest)

    commits = list(iter_commits(dest, blobless=True))
    assert commits
    assert all(f.insertions == 0 and f.deletions == 0 for c in commits for f in c.files)


# ------------------------------------------------------------------ parser

def test_a_binary_file_is_marked_and_carries_no_line_counts(tmp_path):
    from git_synapse.ingest.parser import iter_commits

    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    (work / "blob.bin").write_bytes(bytes(range(256)) * 8)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "binary"], cwd=work, check=True, env=ENV)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)

    f = list(iter_commits(bare))[0].files[0]
    assert f.is_binary is True
    assert f.insertions == 0 and f.deletions == 0


def test_a_commit_with_an_empty_tree_is_kept_with_no_files(tmp_path):
    """An empty commit is real history; dropping it would shift the population."""
    from git_synapse.ingest.parser import iter_commits

    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    (work / "a.txt").write_text("a")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "first"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "--allow-empty", "-m", "empty"],
                   cwd=work, check=True, env=ENV)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)

    commits = {c.subject: c for c in iter_commits(bare)}
    assert "empty" in commits
    assert commits["empty"].files == []


# ------------------------------------------------------------------ predict

def test_impact_chains_respect_max_depth(db):
    from git_synapse.analysis import predict
    from git_synapse.db.engine import query

    for row in query("SELECT DISTINCT source_repo_id s FROM repo_impact LIMIT 5"):
        for chain in predict.impact_chains(row["s"], max_depth=2, limit=5):
            assert chain["depth"] <= 2


def test_chains_are_ranked_by_path_confidence(db):
    from git_synapse.analysis import predict
    from git_synapse.db.engine import query_one

    row = query_one("SELECT DISTINCT target_repo_id t FROM repo_impact LIMIT 1")
    if row is None:
        pytest.skip("no impact rows")
    chains = predict.upstream_chains(row["t"], limit=10)
    scores = [float(c["path_score"]) for c in chains]
    assert scores == sorted(scores, reverse=True)
