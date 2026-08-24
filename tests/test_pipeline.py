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


# ------------------------------------------------------- contention retries

def test_a_repo_that_loses_a_deadlock_is_retried(db, monkeypatch):
    """Postgres resolves a deadlock by killing one participant. The victim's
    work is still valid, so it is retried rather than reported as failed."""
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import RepoResult

    monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None, raising=False)
    attempts = {"n": 0}

    def flaky(record, force_full=False):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return RepoResult(full_name=record.full_name, status="failed",
                              error="DeadlockDetected: deadlock detected")
        return RepoResult(full_name=record.full_name, status="success")

    monkeypatch.setattr(pipeline, "_sync_repo_once", flaky)
    record = RepoRecord(github_id=1, owner="t", name="x", full_name="t/x",
                        clone_url="", default_branch="main")
    result = pipeline.sync_repo(record)
    assert result.status == "success"
    assert attempts["n"] == 2, "a contention failure must be retried once"


def test_a_repo_that_keeps_losing_is_reported_failed_not_retried_forever(db, monkeypatch):
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import DB_CONTENTION_RETRIES, RepoResult

    monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None, raising=False)
    attempts = {"n": 0}

    def always_deadlock(record, force_full=False):
        attempts["n"] += 1
        return RepoResult(full_name=record.full_name, status="failed",
                          error="deadlock detected")

    monkeypatch.setattr(pipeline, "_sync_repo_once", always_deadlock)
    record = RepoRecord(github_id=1, owner="t", name="x", full_name="t/x",
                        clone_url="", default_branch="main")
    result = pipeline.sync_repo(record)
    assert result.status == "failed"
    assert attempts["n"] == DB_CONTENTION_RETRIES


def test_an_ordinary_failure_is_not_retried(db, monkeypatch):
    """Retrying an auth error or a missing repository only wastes the timeout."""
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import RepoResult

    attempts = {"n": 0}

    def auth_failure(record, force_full=False):
        attempts["n"] += 1
        return RepoResult(full_name=record.full_name, status="failed",
                          error="remote: Invalid username or token.")

    monkeypatch.setattr(pipeline, "_sync_repo_once", auth_failure)
    record = RepoRecord(github_id=1, owner="t", name="x", full_name="t/x",
                        clone_url="", default_branch="main")
    assert pipeline.sync_repo(record).status == "failed"
    assert attempts["n"] == 1


# ------------------------------------------------------- the circuit breaker

def test_a_run_gives_up_once_the_network_is_clearly_down(db, monkeypatch):
    """Four retries at a two-minute timeout is nine minutes per repository, so
    grinding the whole corpus took twenty-five minutes to accomplish nothing."""
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import NETWORK_FAILURE_ABORT, RepoResult

    monkeypatch.setattr(pipeline, "verify_credentials", lambda: "ok")
    monkeypatch.setattr(pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)

    attempted = {"n": 0}

    def always_offline(record, force_full=False):
        attempted["n"] += 1
        return RepoResult(
            full_name=record.full_name, status="failed",
            error="fatal: unable to access: Failed to connect to github.com port 443",
        )

    monkeypatch.setattr(pipeline, "sync_repo", always_offline)

    aborted = []
    real_error = pipeline.log.error

    def capture(msg, *a, **kw):
        if "aborting run" in str(msg):
            aborted.append(msg)
        return real_error(msg, *a, **kw)

    monkeypatch.setattr(pipeline.log, "error", capture)

    records = [
        RepoRecord(github_id=i, owner="t", name=f"r{i}", full_name=f"t/r{i}",
                   clone_url="", default_branch="main")
        for i in range(NETWORK_FAILURE_ABORT * 3)
    ]
    pipeline.run_ingest(records=records, trigger="test")

    # Assert the breaker fired, not how many futures happened to be in flight
    # when it did: the executor submits everything up front, so the count that
    # slips through is a scheduling detail rather than the behaviour under test.
    assert aborted, "the breaker never fired despite the network being down"


def test_an_isolated_failure_does_not_trip_the_breaker(db, monkeypatch):
    """A few bad repositories among good ones is normal, and must not abort."""
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import RepoResult

    monkeypatch.setattr(pipeline, "verify_credentials", lambda: "ok")
    monkeypatch.setattr(pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)

    seen = []

    def mostly_fine(record, force_full=False):
        seen.append(record.full_name)
        if len(seen) % 5 == 0:
            return RepoResult(full_name=record.full_name, status="failed",
                              error="fatal: unable to access: Failed to connect")
        return RepoResult(full_name=record.full_name, status="success")

    monkeypatch.setattr(pipeline, "sync_repo", mostly_fine)
    records = [
        RepoRecord(github_id=i, owner="t", name=f"s{i}", full_name=f"t/s{i}",
                   clone_url="", default_branch="main")
        for i in range(30)
    ]
    pipeline.run_ingest(records=records, trigger="test")
    assert len(seen) == 30, "an occasional failure aborted the whole run"


def test_one_repository_raising_does_not_kill_the_run(db, monkeypatch):
    """sync_repo catches its own errors; this is the belt-and-braces path."""
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import RepoResult

    monkeypatch.setattr(pipeline, "verify_credentials", lambda: "ok")
    monkeypatch.setattr(pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)

    def explode_once(record, force_full=False):
        if record.name == "boom":
            raise RuntimeError("unexpected")
        return RepoResult(full_name=record.full_name, status="success")

    monkeypatch.setattr(pipeline, "sync_repo", explode_once)
    records = [
        RepoRecord(github_id=1, owner="t", name="boom", full_name="t/boom",
                   clone_url="", default_branch="main"),
        RepoRecord(github_id=2, owner="t", name="fine", full_name="t/fine",
                   clone_url="", default_branch="main"),
    ]
    result = pipeline.run_ingest(records=records, trigger="test")
    assert len(result.repos) == 2
    assert any(r.status == "failed" for r in result.repos)


# ------------------------------------------------- the global derived stages
#
# Six global rebuilds run after the per-repo fan-out. Each is wrapped in its own
# try/except on purpose: the per-repo results are already committed, so one
# global stage failing must not discard them or the other five. Nothing had ever
# executed those except arms, which is precisely where that promise could be
# broken by a re-raise or a mis-ordered dependency.

_STAGES = [
    ("git_synapse.analysis.crossrepo", "rebuild"),
    ("git_synapse.analysis.depbump", "rebuild"),
    ("git_synapse.analysis.depbump", "refresh_declared"),
    ("git_synapse.analysis.depbump", "refresh_modules"),
    ("git_synapse.analysis.lagged", "rebuild"),
    ("git_synapse.analysis.predict", "rebuild"),
    ("git_synapse.analysis.mining", "rebuild"),
]


class _Anything:
    """Stands in for every stage's stats object: any attribute reads as 0."""

    def __getattr__(self, name):
        return 0


def _stub_run(monkeypatch):
    monkeypatch.setattr(pipeline, "verify_credentials", lambda: "ok")
    monkeypatch.setattr(pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)


def test_every_derived_stage_runs_once_the_corpus_changed(db, monkeypatch):
    _stub_run(monkeypatch)
    called = []
    for mod, fn in _STAGES:
        monkeypatch.setattr(f"{mod}.{fn}",
                            lambda *a, _n=f"{mod}.{fn}", **k: called.append(_n) or _Anything())

    result = pipeline.run_ingest(records=[], trigger="test", force_full=True)
    assert result.status in ("success", "partial")
    assert called == [f"{m}.{f}" for m, f in _STAGES]


def test_a_failing_derived_stage_does_not_stop_the_ones_after_it(db, monkeypatch):
    """The whole reason each stage is guarded separately."""
    _stub_run(monkeypatch)
    reached = []

    def boom(*a, **k):
        raise RuntimeError("stage exploded")

    for mod, fn in _STAGES:
        monkeypatch.setattr(f"{mod}.{fn}", boom)
        monkeypatch.setattr(f"{mod}.{fn}",
                            lambda *a, _n=f"{mod}.{fn}", **k: reached.append(_n) or boom())

    result = pipeline.run_ingest(records=[], trigger="test", force_full=True)
    # Every guarded group was attempted and none of them escaped. refresh_modules
    # is absent by design: it shares a try block with refresh_declared, so it is
    # skipped when that one raises rather than running on a half-refreshed table.
    assert reached == [f"{m}.{f}" for m, f in _STAGES
                       if f != "refresh_modules"]
    assert result.run_id is not None
    assert result.status in ("success", "partial")


def test_the_derived_stages_are_skipped_when_nothing_changed(db, monkeypatch):
    """Six global rebuilds over the whole corpus are not free; a sync that added
    no commits must not pay for them."""
    _stub_run(monkeypatch)
    for mod, fn in _STAGES:
        monkeypatch.setattr(f"{mod}.{fn}",
                            lambda *a, **k: pytest.fail(f"{mod}.{fn} ran with no new commits"))

    result = pipeline.run_ingest(records=[], trigger="test", force_full=False)
    assert result.commits_added == 0
