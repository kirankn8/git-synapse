"""Ingest orchestration: the guards, not the happy path.

Everything destructive lives here -- deleting commits, re-cloning mirrors,
aborting runs. The failures that matter are the quiet ones: a run that reports
success having done nothing, a partial listing accepted as fact, a network blip
treated as a damaged mirror.
"""
from __future__ import annotations

import subprocess

import pytest

from git_synapse.ingest import pipeline
from git_synapse.ingest.pipeline import (
    DISCOVERY_SHRINK_FLOOR,
    NETWORK_FAILURE_ABORT,
    _drop_unreachable_commits,
    _is_contention,
)


# ------------------------------------------------------- contention detection

@pytest.mark.parametrize(
    "error",
    ["deadlock detected", "DeadlockDetected: deadlock detected",
     "could not serialize access due to concurrent update",
     "SerializationFailure"],
)
def test_contention_is_recognised_and_retried(error):
    assert _is_contention(error)


@pytest.mark.parametrize(
    "error",
    [None, "", "authentication failed", "repository not found",
     "fatal: unable to access: Could not connect to server"],
)
def test_a_non_contention_error_is_not_retried(error):
    """Retrying an auth failure or a missing repo just wastes the timeout."""
    assert not _is_contention(error)


# --------------------------------------------------- unreachable-commit sweep

def _commit_repo(tmp_path, n: int):
    work = tmp_path / "w"
    work.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "init", "--quiet", str(work)], check=True)
    for i in range(n):
        (work / f"f{i}.txt").write_text(str(i))
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=env)
        subprocess.run(["git", "commit", "--quiet", "-m", f"c{i}"],
                       cwd=work, check=True, env=env)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)], check=True)
    return bare


def test_sweep_is_a_no_op_when_the_repo_has_no_stored_commits(tmp_path):
    """The cheap count check must gate the expensive walk."""
    assert _drop_unreachable_commits(repo_id=-1, mirror=_commit_repo(tmp_path, 2)) == 0


def test_sweep_tolerates_a_missing_mirror(tmp_path):
    """A mirror that is not there must not raise mid-run."""
    assert _drop_unreachable_commits(repo_id=-1, mirror=tmp_path / "absent.git") == 0


# ------------------------------------------------------------ discovery guard

def test_discovery_refuses_a_collapsed_listing(db, monkeypatch):
    """An unauthenticated request returns HTTP 200 and only public repositories
    -- 59 of 272 here -- and discovery accepted it silently."""
    from git_synapse.db.engine import query_one
    from git_synapse.ingest.pipeline import AuthError

    known = query_one("SELECT count(*) AS n FROM repo WHERE is_enabled")["n"]
    if known < 10:
        pytest.skip("needs a populated corpus")

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def list_org_repos(self): return []

    monkeypatch.setattr(pipeline, "GitHubClient", lambda cfg: _Client())
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg: [])

    with pytest.raises(AuthError, match="already known"):
        pipeline.discover()


def test_discovery_accepts_a_listing_that_is_merely_smaller(db, monkeypatch):
    """Repositories do get archived; only a collapse is suspicious."""
    from git_synapse.db.engine import query_one

    known = query_one("SELECT count(*) AS n FROM repo WHERE is_enabled")["n"]
    if known < 10:
        pytest.skip("needs a populated corpus")

    keep = int(known * DISCOVERY_SHRINK_FLOOR) + 1
    fake = [object()] * keep

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def list_org_repos(self): return fake

    monkeypatch.setattr(pipeline, "GitHubClient", lambda cfg: _Client())
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg: fake)
    monkeypatch.setattr(pipeline, "upsert_repo", lambda record, conn: None)

    assert len(pipeline.discover()) == keep


# --------------------------------------------------------------- run lifecycle



def test_the_network_abort_threshold_exceeds_the_worker_count(db):
    """A single unlucky burst must not trip the circuit breaker."""
    from git_synapse.config import get_config

    assert NETWORK_FAILURE_ABORT > get_config().ingest.concurrency


def test_load_repo_records_returns_usable_records(db):
    records = pipeline.load_repo_records()
    if not records:
        pytest.skip("no repositories stored")
    r = records[0]
    assert r.full_name and "/" in r.full_name
    assert r.clone_url.startswith("http")


# ------------------------------------------------------ credential preflight

@pytest.fixture
def token(monkeypatch):
    """Control what current_token() returns; Config is frozen, so patch the class."""
    from git_synapse.config import GitHubConfig

    def _set(value: str):
        monkeypatch.setattr(GitHubConfig, "current_token", lambda self: value)

    return _set


def test_an_empty_credential_is_refused_before_any_mirror_is_touched(db, token):
    from git_synapse.ingest.pipeline import AuthError, verify_credentials

    token("")
    with pytest.raises(AuthError, match="empty"):
        verify_credentials()


def test_a_rejected_credential_says_it_expired_and_that_nothing_was_touched(
    db, token, monkeypatch
):
    """The message is the whole value here: it must send someone at the token,
    not at the mirrors."""
    import httpx

    token("ghu_" + "x" * 36)
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **kw: httpx.Response(401, json={"message": "Bad credentials"},
                                        request=httpx.Request("GET", "https://api.github.com")),
    )
    from git_synapse.ingest.pipeline import AuthError, verify_credentials

    with pytest.raises(AuthError) as exc:
        verify_credentials()
    assert "401" in str(exc.value)
    assert "mirrors" in str(exc.value)


def test_a_server_error_during_verification_also_aborts(db, token, monkeypatch):
    import httpx

    token("ghu_" + "x" * 36)
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **kw: httpx.Response(503, json={},
                                        request=httpx.Request("GET", "https://api.github.com")),
    )
    from git_synapse.ingest.pipeline import AuthError, verify_credentials

    with pytest.raises(AuthError):
        verify_credentials()


def test_an_unreachable_api_does_not_abort_the_run(db, token, monkeypatch):
    """GitHub being briefly unreachable is not evidence the token is bad."""
    import httpx

    token("ghu_" + "x" * 36)

    def unreachable(*a, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "get", unreachable)
    from git_synapse.ingest.pipeline import verify_credentials

    assert verify_credentials() == "unverified"


def test_a_valid_credential_reports_the_login(db, token, monkeypatch):
    import httpx

    token("ghu_" + "x" * 36)
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **kw: httpx.Response(200, json={"login": "someone"},
                                        request=httpx.Request("GET", "https://api.github.com")),
    )
    from git_synapse.ingest.pipeline import verify_credentials

    assert verify_credentials() == "someone"
