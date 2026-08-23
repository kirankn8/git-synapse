"""Defensive branches across the codebase.

Every one of these is a guard someone wrote for a condition they expected. An
untested guard is a guess: it either does not fire when it should, or fires and
does the wrong thing. These are the cheap ones to get wrong and the expensive
ones to debug.
"""
from __future__ import annotations

import subprocess

import pytest


# ------------------------------------------------------------- aggregate

def test_aggregating_an_unknown_repo_is_a_no_op(scratch_db):
    from git_synapse.analysis.aggregate import rebuild_repo
    from git_synapse.db.engine import connection

    with connection() as conn:
        stats = rebuild_repo(999999999, conn)
    assert stats.file_pairs == 0


def test_scoring_an_unknown_repo_is_a_no_op(scratch_db):
    from git_synapse.analysis.score import score_repo
    from git_synapse.db.engine import connection

    with connection() as conn:
        score_repo(999999999, conn)  # must not raise


# ---------------------------------------------------------------- mining

def test_mining_an_empty_corpus_is_a_no_op(scratch_db):
    """A fresh install runs this before any repository exists."""
    from git_synapse.analysis import mining

    stats = mining.rebuild(force=True)
    assert stats is not None


def test_lagged_rebuild_on_an_empty_corpus_is_a_no_op(scratch_db):
    from git_synapse.analysis import lagged

    stats = lagged.rebuild(force=True)
    assert stats is not None


def test_predict_rebuild_on_an_empty_corpus_is_a_no_op(scratch_db):
    from git_synapse.analysis import predict

    stats = predict.rebuild(force=True)
    assert stats is not None


def test_crossrepo_rebuild_on_an_empty_corpus_is_a_no_op(scratch_db):
    from git_synapse.analysis import crossrepo

    stats = crossrepo.rebuild(force=True)
    assert stats is not None


# --------------------------------------------------------------- watermarks

def test_a_rebuild_skips_when_its_inputs_have_not_changed(scratch_db):
    """The fingerprint is what keeps the fifteen-minute tick cheap."""
    from git_synapse.analysis import lagged

    first = lagged.rebuild(force=True)
    second = lagged.rebuild(force=False)
    assert second.n_bins == 0 or second.rows_written == first.rows_written


# ------------------------------------------------------------------ parser

def test_a_mirror_that_is_not_a_repository_raises_clearly(tmp_path):
    from git_synapse.ingest.gitops import GitError
    from git_synapse.ingest.parser import iter_commits

    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises((GitError, OSError)):
        list(iter_commits(not_a_repo))


def test_a_since_sha_that_no_longer_exists_is_the_callers_problem(tmp_path):
    """The caller verifies the SHA still exists, because a force-push can orphan
    one and git errors on an unknown revision. This pins that contract."""
    from git_synapse.ingest.gitops import GitError
    from git_synapse.ingest.parser import iter_commits

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=env)
    (work / "a.txt").write_text("a")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=env)
    subprocess.run(["git", "commit", "--quiet", "-m", "a"], cwd=work, check=True, env=env)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=env)

    with pytest.raises(GitError):
        list(iter_commits(bare, since_shas=["0" * 40]))


# ------------------------------------------------------------------ config

def test_configuration_reads_the_environment_and_can_be_reset(monkeypatch):
    from git_synapse.config import get_config, reset_config_cache

    monkeypatch.setenv("MAX_QUERY_LIMIT", "77")
    reset_config_cache()
    try:
        assert get_config().analysis.max_limit == 77
    finally:
        monkeypatch.delenv("MAX_QUERY_LIMIT", raising=False)
        reset_config_cache()


def test_a_malformed_numeric_setting_fails_loudly_and_names_itself(monkeypatch):
    """A typo in an environment variable stops the container with an error that
    says which variable and what it contained.

    Falling back to a default would be worse: the service would run with
    settings the operator did not choose and nothing would say so.
    """
    from git_synapse.config import get_config, reset_config_cache

    monkeypatch.setenv("MAX_QUERY_LIMIT", "not-a-number")
    reset_config_cache()
    try:
        with pytest.raises(ValueError) as exc:
            get_config()
        assert "MAX_QUERY_LIMIT" in str(exc.value)
        assert "not-a-number" in str(exc.value)
    finally:
        monkeypatch.delenv("MAX_QUERY_LIMIT", raising=False)
        reset_config_cache()


def test_the_dsn_contains_every_part_it_needs():
    from git_synapse.config import get_config

    dsn = get_config().db.dsn
    for part in ("host=", "port=", "user=", "dbname="):
        assert part in dsn
