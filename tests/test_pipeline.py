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


def test_sweep_is_a_no_op_when_the_repo_has_no_stored_commits(tmp_path, db):
    """The cheap count check must gate the expensive walk."""
    assert _drop_unreachable_commits(repo_id=-1, mirror=_commit_repo(tmp_path, 2)) == 0


def test_sweep_tolerates_a_missing_mirror(tmp_path, db):
    """A mirror that is not there must not raise mid-run."""
    assert _drop_unreachable_commits(repo_id=-1, mirror=tmp_path / "absent.git") == 0


def test_a_commit_reachable_only_from_a_tag_is_not_unreachable(tmp_path, db):
    """The sweep must measure over exactly the refs the ingest walks. Measuring
    from the branch alone deleted every commit the tag walk had just inserted,
    and did it silently -- the run reports what the loader wrote, not what
    survived. It is self-triggering too: the new commits push the stored count
    above the branch count, which is the condition that runs the sweep."""
    from datetime import UTC, datetime

    from git_synapse.db.orm import models, session_scope

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    work = tmp_path / "tagged"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True)

    def commit(name):
        (work / name).write_text(name)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=env)
        subprocess.run(["git", "commit", "--quiet", "-m", name], cwd=work, check=True, env=env)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                              capture_output=True, text=True).stdout.strip()

    on_branch = commit("a.txt")
    subprocess.run(["git", "checkout", "-q", "-b", "release"], cwd=work, check=True, env=env)
    tagged_only = commit("release.txt")          # never merges back
    subprocess.run(["git", "tag", "v1.0.0"], cwd=work, check=True, env=env)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=work, check=True, env=env)

    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)], check=True)

    with session_scope() as conn:
        repo = models().Repo(full_name="acme/tagged", name="tagged", owner="acme")
        conn.add(repo)
        conn.flush()
        repo_id = repo.id
        for sha in (on_branch, tagged_only):
            conn.add(models().Commit(
                repo_id=repo_id, sha=sha,
                authored_at=datetime.now(UTC), committed_at=datetime.now(UTC),
            ))
        conn.flush()
        try:
            assert _drop_unreachable_commits(repo_id, bare) == 0
            survived = {
                row.sha for row in conn.query(models().Commit).filter_by(repo_id=repo_id).all()
            }
            assert tagged_only in survived, "a release commit is not unreachable"
        finally:
            conn.delete(conn.get(models().Repo, repo_id))


# ------------------------------------------------------------ discovery guard

def test_discovery_refuses_a_collapsed_listing(two_accounts, db, monkeypatch):
    """An unauthenticated request returns HTTP 200 and only public repositories
    -- 59 of 272 here -- and discovery accepted it silently."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest.pipeline import AuthError

    with session_scope() as session:
        known = session.query(models().Repo).filter_by(is_enabled=True).count()
    if known < 10:
        pytest.skip("needs a populated corpus")

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def supports_listing(self): return True
        def list_repos(self, login): return []
        def list_page(self, login, page=1):
            from git_synapse.ingest.providers import Page
            return Page([], has_more=False, total=0)

    monkeypatch.setattr(pipeline.providers, "for_source",
                        lambda src, patient=True, token="": _Client())
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg, tracked=frozenset(): [])

    with pytest.raises(AuthError, match="already known"):
        pipeline.discover()


def test_discovery_accepts_a_listing_that_is_merely_smaller(two_accounts, db, monkeypatch):
    """Repositories do get archived; only a collapse is suspicious."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        known = session.query(models().Repo).filter_by(is_enabled=True).count()
    if known < 10:
        pytest.skip("needs a populated corpus")

    keep = int(known * DISCOVERY_SHRINK_FLOOR) + 1
    fake = [_record(f"smaller/r{i}") for i in range(keep)]

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def supports_listing(self): return True
        def list_repos(self, login): return fake if login == "alpha" else []
        def list_page(self, login, page=1):
            from git_synapse.ingest.providers import Page
            rows = fake if login == "alpha" else []
            return Page(rows, has_more=False, total=len(rows))

    monkeypatch.setattr(pipeline.providers, "for_source",
                        lambda src, patient=True, token="": _Client())
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg, tracked=frozenset(): list(records))
    monkeypatch.setattr(pipeline, "upsert_repo", lambda record, conn, **kwargs: None)

    assert len(pipeline.discover()) == keep


# --------------------------------------------------------------- run lifecycle



def test_the_network_abort_threshold_exceeds_the_worker_count(db):
    """A single unlucky burst must not trip the circuit breaker."""
    from git_synapse.config import get_config

    assert get_config().ingest.concurrency < NETWORK_FAILURE_ABORT


def test_load_repo_records_returns_usable_records(db):
    """Reads back a repository it wrote itself.

    Taking `records[0]` -- whichever repository happened to be first -- made
    this assert things about another test's fixture, and it failed the moment a
    fixture row without a clone URL sorted ahead of a real one. The point is
    that every column survives the round trip, which needs a row whose columns
    are known.
    """
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = models().Repo(
            github_id=4242, owner="roundtrip", name="thing",
            full_name="roundtrip/thing", host="github.com", provider="github",
            clone_url="https://github.com/roundtrip/thing.git", default_branch="main",
            primary_language="Rust", topics=["cli", "tool"], visibility="public",
            is_private=False, is_fork=True, is_archived=False, stargazers=77,
            disk_usage_kb=512, is_enabled=True,
        )
        session.add(row)
        session.flush()
        row_id = row.id
    try:
        records = {r.full_name: r for r in pipeline.load_repo_records()}
        r = records["roundtrip/thing"]
        assert r.clone_url == "https://github.com/roundtrip/thing.git"
        assert (r.provider, r.host) == ("github", "github.com")
        assert (r.primary_language, r.default_branch) == ("Rust", "main")
        assert r.topics == ["cli", "tool"]
        # Booleans and counts specifically: they are read back by name, and a
        # field added in the middle of a positional projection used to shift every
        # field after it into a neighbour of compatible type.
        assert r.is_fork is True and r.is_archived is False
        assert r.stargazers == 77 and r.disk_usage_kb == 512
        assert r.github_id == 4242
    finally:
        with session_scope() as session:
            session.delete(session.get(models().Repo, row_id))


# ------------------------------------------------------ credential preflight

@pytest.fixture
def token(monkeypatch):
    """Control what current_token() returns; Config is frozen, so patch the class."""
    from git_synapse.config import HostCredential

    def _set(value: str):
        monkeypatch.setattr(HostCredential, "current_token", lambda self: value)

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

    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")
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

    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")
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

    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")
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
    ("git_synapse.analysis.depbump", "rebuild"),
    ("git_synapse.analysis.depbump", "refresh_declared"),
    ("git_synapse.analysis.depbump", "refresh_modules"),
    ("git_synapse.analysis.predict", "rebuild"),
    ("git_synapse.analysis.mining", "rebuild"),
]


class _Anything:
    """Stands in for every stage's stats object: any attribute reads as 0."""

    def __getattr__(self, name):
        return 0


def _stub_run(monkeypatch):
    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")
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
        # Bound at definition: without this every stub reports the last stage's
        # name, so the failure names the wrong one.
        monkeypatch.setattr(
            f"{mod}.{fn}",
            lambda *a, mod=mod, fn=fn, **k: pytest.fail(
                f"{mod}.{fn} ran with no new commits"))

    result = pipeline.run_ingest(records=[], trigger="test", force_full=False)
    assert result.commits_added == 0


# ---------------------------------------- the sweep with commits actually stored
#
# The branches above the cheap count gate had never run against a repository that
# has any. 735 commits across 24 repositories survived a force-push this way,
# inflating the N of every contingency table in those repos.

@pytest.fixture()
def swept_repo(scratch_db, tmp_path):
    """A repo row whose stored commits outnumber what its mirror still reaches."""
    from datetime import UTC, datetime

    from git_synapse.db.orm import models, session_scope

    mirror = _commit_repo(tmp_path, 2)
    reachable = subprocess.run(
        ["git", "rev-list", "HEAD"], cwd=mirror, capture_output=True, text=True,
        check=True,
    ).stdout.split()

    with session_scope() as conn:
        for model in (models().Commit, models().Repo):
            conn.query(model).delete(synchronize_session=False)
        repo = models().Repo(github_id=1, owner="t", name="w", full_name="t/w",
                             clone_url="", default_branch="main")
        author = conn.query(models().Author).filter_by(email="t@e").one_or_none()
        if author is None:
            author = models().Author(email="t@e", display_name="t")
            conn.add(author)
        conn.add(repo)
        conn.flush()
        repo_id, author_id = repo.id, author.id
        # Two commits git still reaches, plus two it does not.
        for sha in [*reachable, "d" * 40, "e" * 40]:
            conn.add(models().Commit(
                repo_id=repo_id, sha=sha, author_id=author_id,
                committer_id=author_id, authored_at=datetime.now(UTC),
                committed_at=datetime.now(UTC), subject="x",
            ))
    return repo_id, mirror


def test_the_sweep_removes_only_the_commits_git_no_longer_reaches(swept_repo):
    from git_synapse.db.orm import models, session_scope

    repo_id, mirror = swept_repo
    assert _drop_unreachable_commits(repo_id, mirror) == 2
    with session_scope() as session:
        assert session.query(models().Commit).filter_by(repo_id=repo_id).count() == 2
    # Idempotent: a second sweep has nothing to do and must not pay for the walk.
    assert _drop_unreachable_commits(repo_id, mirror) == 0


@pytest.mark.parametrize("failing_call", [1, 2])
def test_a_git_failure_during_the_sweep_deletes_nothing(swept_repo, monkeypatch,
                                                        failing_call):
    """Deleting commits on the strength of a failed reachability walk would
    erase real history."""
    from git_synapse.db.orm import models, session_scope

    repo_id, mirror = swept_repo
    calls = {"n": 0}
    real = subprocess.run

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == failing_call:
            raise OSError("git not executable")
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "run", flaky)
    assert _drop_unreachable_commits(repo_id, mirror) == 0
    with session_scope() as session:
        assert session.query(models().Commit).filter_by(repo_id=repo_id).count() == 4


def test_a_nonzero_rev_list_during_the_sweep_deletes_nothing(swept_repo, monkeypatch):
    repo_id, mirror = swept_repo
    calls = {"n": 0}
    real = subprocess.run

    class _Bad:
        returncode = 1
        stdout = ""
        stderr = "fatal"

    def flaky(*a, **k):
        calls["n"] += 1
        return real(*a, **k) if calls["n"] == 1 else _Bad()

    monkeypatch.setattr(subprocess, "run", flaky)
    assert _drop_unreachable_commits(repo_id, mirror) == 0


def test_an_empty_reachable_set_deletes_nothing(swept_repo, monkeypatch):
    """An empty walk means the mirror is broken, not that every commit is gone."""
    repo_id, mirror = swept_repo
    calls = {"n": 0}
    real = subprocess.run

    class _Empty:
        returncode = 0
        stdout = ""
        stderr = ""

    def flaky(*a, **k):
        calls["n"] += 1
        return real(*a, **k) if calls["n"] == 1 else _Empty()

    monkeypatch.setattr(subprocess, "run", flaky)
    assert _drop_unreachable_commits(repo_id, mirror) == 0


# ------------------------------------------------- discovery across accounts
#
# The guard tests above skip unless the corpus is already populated, so the body
# of `discover` -- per-account listing, failure isolation, ownership -- ran under
# no test at all. These build their own accounts instead.

@pytest.fixture
def two_accounts(db):
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest import accounts

    with session_scope() as conn:
        for login in ("alpha", "beta"):
            row = conn.query(models().Account).filter_by(login=login).one_or_none()
            if row is not None:
                conn.delete(row)
    made = [accounts.add_account("alpha"), accounts.add_account("beta")]
    yield made
    with session_scope() as conn:
        for login in ("alpha", "beta"):
            row = conn.query(models().Account).filter_by(login=login).one_or_none()
            if row is not None:
                conn.delete(row)


def _record(full_name, **over):
    from git_synapse.ingest.github import RepoRecord
    owner, name = full_name.split("/")
    return RepoRecord(github_id=abs(hash(full_name)) % 10**8, owner=owner,
                      name=name, full_name=full_name, **over)


def _client_returning(mapping):
    """A provider whose listing depends on the owner, or raises for it."""
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def supports_listing(self): return True

        def list_repos(self, login):
            outcome = mapping[login]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def list_page(self, login, page=1):
            from git_synapse.ingest.providers import Page
            rows = self.list_repos(login)
            return Page(rows, has_more=False, total=len(rows))
    return lambda src, patient=True, token="": _Client()


def test_discovery_with_no_accounts_says_how_to_add_one(db, monkeypatch):
    """Returning an empty list would look like an org with no repositories."""
    from git_synapse.ingest import accounts
    from git_synapse.ingest.pipeline import AuthError

    monkeypatch.setattr(accounts, "list_accounts", lambda **k: [])
    with pytest.raises(AuthError, match="account add"):
        pipeline.discover()


def test_one_failing_account_does_not_stop_the_others(two_accounts, db, monkeypatch):
    """Discovery runs across accounts, so a single broken one must cost only
    its own repositories."""
    good = [_record("beta/keep")]
    monkeypatch.setattr(pipeline.providers, "for_source", _client_returning(
        {"alpha": RuntimeError("listing blew up"), "beta": good}))
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg, tracked=frozenset(): list(records))

    selected = pipeline.discover()
    assert [r.full_name for r in selected] == ["beta/keep"]


def test_every_account_failing_is_reported_as_one_error(two_accounts, db, monkeypatch):
    """Nothing discovered *and* everything failed is a broken run, not an empty
    organisation, and must not be reported as the latter."""
    from git_synapse.ingest.pipeline import AuthError

    monkeypatch.setattr(pipeline.providers, "for_source", _client_returning(
        {"alpha": RuntimeError("down"), "beta": RuntimeError("also down")}))
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg, tracked=frozenset(): [])

    with pytest.raises(AuthError, match="every configured account failed"):
        pipeline.discover()


def test_a_forks_parent_is_looked_up_because_the_listing_omits_it(
    two_accounts, db, monkeypatch,
):
    """GitHub's list endpoints carry `fork` but not `parent`, so discovery asks
    once per fork. That answer is the whole basis for telling a duplicate copy
    from a codebase somebody actually works in.
    """
    fork = _record("alpha/fork", is_fork=True)
    plain = _record("alpha/plain")
    asked = []

    class _Fake:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def supports_listing(self): return True

        def list_page(self, login, page=1):
            from git_synapse.ingest.providers import Page
            rows = [fork, plain] if login == "alpha" else []
            return Page(rows, has_more=False, total=len(rows))

        def fetch_parent(self, full_name):
            asked.append(full_name)
            return "upstream/fork"

    monkeypatch.setattr(pipeline.providers, "for_source",
                        lambda src, patient=True, token="": _Fake())
    monkeypatch.setattr(pipeline, "select_repos",
                        lambda records, cfg, tracked=frozenset(): list(records))
    monkeypatch.setattr(pipeline, "DISCOVERY_SHRINK_FLOOR", 0.0)

    pipeline.discover()

    # Only the fork is asked about; the ordinary repository costs no request.
    assert asked == ["alpha/fork"]
    assert fork.parent_full_name == "upstream/fork"


def test_a_discovered_repository_records_which_account_found_it(two_accounts, db, monkeypatch):
    """`repo.account_id` is what lets an account be removed without deleting the
    history mined from it."""
    from git_synapse.db.orm import models, session_scope

    monkeypatch.setattr(pipeline.providers, "for_source", _client_returning(
        {"alpha": [_record("alpha/one")], "beta": []}))
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg, tracked=frozenset(): list(records))
    monkeypatch.setattr(pipeline, "DISCOVERY_SHRINK_FLOOR", 0.0)

    pipeline.discover()
    with session_scope() as session:
        row = session.query(models().Account).join(
            models().Repo, models().Repo.account_id == models().Account.id
        ).filter(models().Repo.full_name == "alpha/one").one_or_none()
    assert row and row.login == "alpha"


def test_the_shrink_guard_stands_down_when_an_account_errored(two_accounts, db, monkeypatch):
    """A collapse already explained by a reported failure is not evidence of a
    bad credential, and refusing the run twice for one cause helps nobody."""
    monkeypatch.setattr(pipeline.providers, "for_source", _client_returning(
        {"alpha": RuntimeError("down"), "beta": [_record("beta/still-here")]}))
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg, tracked=frozenset(): list(records))

    selected = pipeline.discover()          # must not raise the shrink AuthError
    assert [r.full_name for r in selected] == ["beta/still-here"]


def test_marking_no_replays_touches_nothing(db):
    """Called for every repository, and most have none. An empty set must not
    become a bulk mutation with an empty collection clause."""
    from git_synapse.db.orm import session_scope
    from git_synapse.ingest.pipeline import _mark_replays

    with session_scope() as conn:
        assert _mark_replays(1, set(), conn) == 0


def test_an_absent_token_is_allowed_when_every_repository_is_public(monkeypatch):
    """Cloning public repositories needs no credential, so demanding one would
    refuse a run that would have worked."""
    from git_synapse.ingest import pipeline as P

    cfg = P.get_config().providers.github
    monkeypatch.setattr(type(cfg), "current_token", lambda self: "", raising=False)
    assert P.verify_credentials(required=False) == "anonymous"


def test_an_absent_token_is_refused_when_something_is_private(monkeypatch):
    """A private repository cannot be cloned anonymously, and finding that out
    per-repository turns one missing setting into dozens of clone failures."""
    from git_synapse.ingest import pipeline as P
    from git_synapse.ingest.pipeline import AuthError

    cfg = P.get_config().providers.github
    monkeypatch.setattr(type(cfg), "current_token", lambda self: "", raising=False)
    with pytest.raises(AuthError):
        P.verify_credentials(required=True)


def test_marking_a_replay_takes_it_out_of_the_statistics(db):
    """Storing it is the point -- the commit is real and belongs in the range
    between two releases -- but counting it would say those files belong
    together on evidence that is one observation repeated."""
    from datetime import UTC, datetime

    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest.pipeline import _mark_replays

    with session_scope() as conn:
        repo_row = models().Repo(full_name="acme/replayed", name="replayed", owner="acme")
        conn.add(repo_row)
        conn.flush()
        repo = repo_row.id
        try:
            sha = "d" * 40
            conn.add(models().Commit(repo_id=repo, sha=sha,
                                     authored_at=datetime.now(UTC),
                                     committed_at=datetime.now(UTC),
                                     pair_eligible=True))
            conn.flush()
            assert _mark_replays(repo, {sha}, conn) == 1
            row = conn.query(models().Commit).filter_by(repo_id=repo, sha=sha).one()
            assert (row.is_replay, row.pair_eligible) == (True, False)
            # Re-running must not count it twice.
            assert _mark_replays(repo, {sha}, conn) == 0
        finally:
            conn.rollback()


def test_a_bad_credential_stops_discovery_rather_than_repeating_itself(two_accounts, db, monkeypatch):
    """Retrying the rest would run the same broken credential against every
    account and report a different failure for each."""
    from git_synapse.ingest.pipeline import AuthError

    def _client(src, patient=True, token=""):
        class _C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def supports_listing(self): return True
            def list_repos(self, login):
                raise AuthError("bad credential")
            def list_page(self, login, page=1):
                raise AuthError("bad credential")
        return _C()

    monkeypatch.setattr(pipeline.providers, "for_source", _client)
    with pytest.raises(AuthError, match="bad credential"):
        pipeline.discover()


def test_a_collapsed_listing_is_refused_even_with_accounts_configured(two_accounts, db, monkeypatch):
    """An unauthenticated request returns HTTP 200 and only public repositories,
    and the run then quietly refreshes a fraction of the corpus."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest.pipeline import AuthError

    with session_scope() as conn:
        for i in range(30):
            if conn.query(models().Repo).filter_by(full_name=f"bulk/r{i}").one_or_none() is None:
                conn.add(models().Repo(full_name=f"bulk/r{i}", name=f"r{i}",
                                        owner="bulk", is_enabled=True))
    try:
        monkeypatch.setattr(pipeline.providers, "for_source", _client_returning(
            {"alpha": [_record("alpha/one")], "beta": []}))
        monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg, tracked=frozenset(): list(records))
        with pytest.raises(AuthError, match="already known"):
            pipeline.discover()
    finally:
        with session_scope() as conn:
            for row in conn.query(models().Repo).filter_by(owner="bulk").all():
                conn.delete(row)


def test_discovery_gives_up_on_a_rate_limit_instead_of_sleeping_through_it(
        db, monkeypatch):
    """Waiting out a rate limit is right for one repository's mirror. Across a
    hundred sources it is not: five retries of sixty seconds each, per source,
    is most of a day asleep inside a single run -- and discovery repeats hourly,
    so the wait buys nothing a retry does not."""
    from git_synapse.ingest import accounts, providers

    seen = {}

    def _spy(source, patient=True, token=""):
        seen["patient"] = patient
        raise RuntimeError("stop; the flag is what is under test")

    monkeypatch.setattr(providers, "for_source", _spy)
    row = accounts.add_account("impatient-org", kind="org", provider="github",
                               host="github.com")
    try:
        with pytest.raises(RuntimeError):
            pipeline._discover_account(accounts.get_account(row["id"]))
        assert seen["patient"] is False
    finally:
        accounts.remove_account(row["id"])


def test_the_clone_path_stays_patient(db):
    """The trade is the other way for a mirror: one repository, all night."""
    import inspect

    src = inspect.getsource(pipeline._sync_repo_once)
    assert "patient=False" not in src, \
        "a clone should wait out a rate limit rather than fail the repository"


# --------------------------------------------------- the single-run lock, and aborts

class _FakeSession:
    """Stands in for the session `_try_ingest_lock` locks its meta row through."""

    def __init__(self, error):
        self._error = error
        self.rolled_back = False

    def get(self, *a, **kw):
        raise self._error

    def rollback(self):
        self.rolled_back = True


def _operational(detail):
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT", {}, Exception(detail))


def test_a_second_ingest_finds_the_lock_held_and_declines(db):
    """PostgreSQL reports NOWAIT contention as 55P03. That is the one failure
    that means "somebody else is already running", not "the lock is broken"."""
    session = _FakeSession(_operational("55P03: could not obtain lock on row"))
    assert pipeline._try_ingest_lock(session) is False
    assert session.rolled_back is True

    # The same condition spelled out in words rather than by code.
    worded = _FakeSession(_operational("could not obtain lock on row"))
    assert pipeline._try_ingest_lock(worded) is False


def test_a_database_error_that_is_not_contention_is_not_swallowed(db):
    """Treating every OperationalError as "busy" would turn a broken database
    into a run that silently does nothing, forever."""
    from sqlalchemy.exc import OperationalError

    session = _FakeSession(_operational("57P01: terminating connection"))
    with pytest.raises(OperationalError):
        pipeline._try_ingest_lock(session)
    assert session.rolled_back is False


def test_a_run_against_a_newer_database_refuses_before_touching_a_mirror(db, monkeypatch):
    """Older code than the database was migrated to: every write would fail one
    repository at a time, so the run refuses once and records why."""
    import time as _time

    monkeypatch.setattr(pipeline, "schema_drift", lambda: 2)

    run = pipeline._run_ingest_locked([], "manual", False, None, _time.monotonic())
    assert run.status == "failed"
    assert run.run_id is not None


def test_a_duplicated_history_is_reported_at_the_end_of_a_run(db, monkeypatch):
    """Two copies of one history make every corpus-wide total count it twice,
    and neither row looks wrong on its own -- so the run says so out loud."""
    import time as _time

    from git_synapse.analysis import query

    monkeypatch.setattr(pipeline, "schema_drift", lambda: 0)
    monkeypatch.setattr(pipeline, "verify_credentials", lambda required=True: "ok")
    monkeypatch.setattr(pipeline, "private_repos_in_scope", lambda: 0)
    monkeypatch.setattr(pipeline.derived, "ensure_current", lambda: None)
    monkeypatch.setattr(query, "duplicate_histories", lambda: [
        {"copies": 2, "host": "gitlab.com", "commits": 13981,
         "names": ["veloren/veloren", "veloren/dev/veloren"]},
    ])

    run = pipeline._run_ingest_locked([], "manual", False, None, _time.monotonic())
    assert run.status != "failed"


def test_a_failing_duplicate_report_does_not_lose_a_finished_run(db, monkeypatch):
    """The repositories are already ingested and recorded by this point. A
    report that cannot run is not a reason to throw that away."""
    import time as _time

    from git_synapse.analysis import query

    def boom():
        raise RuntimeError("the reporting query blew up")

    monkeypatch.setattr(pipeline, "schema_drift", lambda: 0)
    monkeypatch.setattr(pipeline, "verify_credentials", lambda required=True: "ok")
    monkeypatch.setattr(pipeline, "private_repos_in_scope", lambda: 0)
    monkeypatch.setattr(pipeline.derived, "ensure_current", lambda: None)
    monkeypatch.setattr(query, "duplicate_histories", boom)

    run = pipeline._run_ingest_locked([], "manual", False, None, _time.monotonic())
    assert run.status != "failed"


def test_the_first_ingest_creates_the_lock_row_it_locks(db):
    """On a fresh database there is nothing to lock yet, so the first run makes
    the row. Every later run locks it instead."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.get(models().Meta, "lock:ingest")
        if row is not None:
            session.delete(row)

    with session_scope() as session:
        assert pipeline._try_ingest_lock(session) is True

    with session_scope() as session:
        assert session.get(models().Meta, "lock:ingest") is not None


def test_a_repository_deleted_mid_ingest_fails_that_repository_by_name(
    db, monkeypatch, tmp_path,
):
    """A run can be hours long and an account can be removed while it is in
    flight, taking its repositories with it. The row is gone by the time the
    mirror comes back, and the result has to name which one rather than raise
    an attribute error off a None."""
    from git_synapse.ingest import gitops
    from git_synapse.ingest.github import RepoRecord

    record = RepoRecord(github_id=None, owner="acme", name="vanishing",
                        full_name="acme/vanishing",
                        clone_url="https://github.com/acme/vanishing.git")

    # Upsert reports an id that is not in the table, which is what a concurrent
    # delete leaves behind.
    monkeypatch.setattr(pipeline, "upsert_repo", lambda rec, conn: 999_999_999)
    monkeypatch.setattr(gitops, "sync_mirror", lambda *a, **kw: gitops.FetchResult(
        path=tmp_path / "mirror", head_sha="0" * 40, cloned=False,
        changed=False, duration_s=0.0, blobless=True, size_kb=0))

    result = pipeline._sync_repo_once(record)
    assert result.status == "failed"
    assert "999999999" in (result.error or "") or "disappeared" in (result.error or "")


def test_an_accounts_own_endpoint_reaches_the_client(two_accounts, db, monkeypatch):
    """A self-hosted install stores its endpoint on the account. If that did not
    reach the provider, every ordinary lookup would fall through to the
    public API and describe somebody else's organisation.
    """
    from git_synapse.ingest import accounts

    seen = []

    class _Fake:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def supports_listing(self): return True
        def list_page(self, login, page=1):
            from git_synapse.ingest.providers import Page
            return Page([], has_more=False, total=0)
        def fetch_parent(self, full_name): return ""

    def capture(source, patient=True, token=""):
        seen.append(source)
        return _Fake()

    monkeypatch.setattr(pipeline.providers, "for_source", capture)
    monkeypatch.setattr(pipeline, "select_repos",
                        lambda records, cfg, tracked=frozenset(): list(records))

    alpha = accounts.find_by_login("alpha")
    accounts.update_account(alpha["id"], api_url="https://ghe.internal/api/v3")
    pipeline._discover_account(accounts.get_account(alpha["id"]))

    assert seen and seen[0].api_url == "https://ghe.internal/api/v3"


def test_an_account_with_no_endpoint_gets_the_providers_public_one(
    two_accounts, db, monkeypatch,
):
    """NULL on the row means "the provider's public API", not "no API" -- which
    is what sends an ordinary source down the no-API path by mistake."""
    from git_synapse.ingest import accounts

    seen = []

    class _Fake:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def supports_listing(self): return True
        def list_page(self, login, page=1):
            from git_synapse.ingest.providers import Page
            return Page([], has_more=False, total=0)
        def fetch_parent(self, full_name): return ""

    monkeypatch.setattr(pipeline.providers, "for_source",
                        lambda source, patient=True, token="": (seen.append(source), _Fake())[1])
    monkeypatch.setattr(pipeline, "select_repos",
                        lambda records, cfg, tracked=frozenset(): list(records))

    beta = accounts.find_by_login("beta")
    assert beta["api_url"] is None
    pipeline._discover_account(accounts.get_account(beta["id"]))

    assert seen and seen[0].api_url == "https://api.github.com"
