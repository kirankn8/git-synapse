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
    from git_synapse.db.engine import connection

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

    with connection() as conn:
        repo_id = conn.execute(
            "INSERT INTO repo (full_name, name, owner) VALUES "
            "('acme/tagged','tagged','acme') RETURNING id").fetchone()[0]
        for sha in (on_branch, tagged_only):
            conn.execute(
                "INSERT INTO commit (repo_id, sha, authored_at, committed_at) "
                "VALUES (%s, %s, now(), now())", (repo_id, sha))
        conn.commit()
        try:
            assert _drop_unreachable_commits(repo_id, bare) == 0
            survived = {r[0] for r in conn.execute(
                "SELECT sha FROM commit WHERE repo_id = %s", (repo_id,)).fetchall()}
            assert tagged_only in survived, "a release commit is not unreachable"
        finally:
            conn.execute("DELETE FROM repo WHERE id = %s", (repo_id,))
            conn.commit()


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
        monkeypatch.setattr(f"{mod}.{fn}",
                            lambda *a, **k: pytest.fail(f"{mod}.{fn} ran with no new commits"))

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
    from git_synapse.db.engine import connection

    mirror = _commit_repo(tmp_path, 2)
    reachable = subprocess.run(
        ["git", "rev-list", "HEAD"], cwd=mirror, capture_output=True, text=True,
        check=True,
    ).stdout.split()

    with connection() as conn:
        conn.execute("TRUNCATE repo RESTART IDENTITY CASCADE")
        repo_id = conn.execute(
            "INSERT INTO repo (github_id, owner, name, full_name, clone_url,"
            " default_branch) VALUES (1,'t','w','t/w','','main') RETURNING id"
        ).fetchone()[0]
        author_id = conn.execute(
            "INSERT INTO author (email, display_name) VALUES ('t@e','t')"
            " ON CONFLICT (email) DO UPDATE SET display_name = 't' RETURNING id"
        ).fetchone()[0]
        # Two commits git still reaches, plus two it does not.
        for sha in [*reachable, "d" * 40, "e" * 40]:
            conn.execute(
                "INSERT INTO commit (repo_id, sha, author_id, committer_id,"
                " authored_at, committed_at, subject) VALUES"
                " (%s,%s,%s,%s,now(),now(),'x')",
                (repo_id, sha, author_id, author_id),
            )
    return repo_id, mirror


def test_the_sweep_removes_only_the_commits_git_no_longer_reaches(swept_repo):
    from git_synapse.db.engine import query_one

    repo_id, mirror = swept_repo
    assert _drop_unreachable_commits(repo_id, mirror) == 2
    left = query_one("SELECT count(*) AS n FROM commit WHERE repo_id = %s", (repo_id,))
    assert left["n"] == 2
    # Idempotent: a second sweep has nothing to do and must not pay for the walk.
    assert _drop_unreachable_commits(repo_id, mirror) == 0


@pytest.mark.parametrize("failing_call", [1, 2])
def test_a_git_failure_during_the_sweep_deletes_nothing(swept_repo, monkeypatch,
                                                        failing_call):
    """Deleting commits on the strength of a failed reachability walk would
    erase real history."""
    from git_synapse.db.engine import query_one

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
    assert query_one("SELECT count(*) AS n FROM commit WHERE repo_id = %s",
                     (repo_id,))["n"] == 4


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
    from git_synapse.db.engine import connection
    from git_synapse.ingest import accounts

    with connection() as conn:
        conn.execute("DELETE FROM account WHERE login IN ('alpha','beta')")
        conn.commit()
    made = [accounts.add_account("alpha"), accounts.add_account("beta")]
    yield made
    with connection() as conn:
        conn.execute("DELETE FROM account WHERE login IN ('alpha','beta')")
        conn.commit()


def _record(full_name):
    from git_synapse.ingest.github import RepoRecord
    owner, name = full_name.split("/")
    return RepoRecord(github_id=abs(hash(full_name)) % 10**8, owner=owner,
                      name=name, full_name=full_name)


def _client_returning(mapping):
    """A GitHubClient whose listing depends on the account, or raises for it."""
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def list_account_repos(self, login, kind):
            outcome = mapping[login]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
    return lambda cfg: _Client()


def test_discovery_with_no_accounts_says_how_to_add_one(db, monkeypatch):
    """Returning an empty list would look like an org with no repositories."""
    from git_synapse.ingest import accounts
    from git_synapse.ingest.pipeline import AuthError

    monkeypatch.setattr(accounts, "list_accounts", lambda **k: [])
    monkeypatch.setattr(accounts, "seed_from_env", lambda: None)
    with pytest.raises(AuthError, match="account add"):
        pipeline.discover()


def test_one_failing_account_does_not_stop_the_others(two_accounts, db, monkeypatch):
    """Discovery runs across accounts, so a single broken one must cost only
    its own repositories."""
    good = [_record("beta/keep")]
    monkeypatch.setattr(pipeline, "GitHubClient", _client_returning(
        {"alpha": RuntimeError("listing blew up"), "beta": good}))
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg: list(records))

    selected = pipeline.discover()
    assert [r.full_name for r in selected] == ["beta/keep"]


def test_every_account_failing_is_reported_as_one_error(two_accounts, db, monkeypatch):
    """Nothing discovered *and* everything failed is a broken run, not an empty
    organisation, and must not be reported as the latter."""
    from git_synapse.ingest.pipeline import AuthError

    monkeypatch.setattr(pipeline, "GitHubClient", _client_returning(
        {"alpha": RuntimeError("down"), "beta": RuntimeError("also down")}))
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg: [])

    with pytest.raises(AuthError, match="every configured account failed"):
        pipeline.discover()


def test_a_discovered_repository_records_which_account_found_it(two_accounts, db, monkeypatch):
    """`repo.account_id` is what lets an account be removed without deleting the
    history mined from it."""
    from git_synapse.db.engine import query_one

    monkeypatch.setattr(pipeline, "GitHubClient", _client_returning(
        {"alpha": [_record("alpha/one")], "beta": []}))
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg: list(records))
    monkeypatch.setattr(pipeline, "DISCOVERY_SHRINK_FLOOR", 0.0)

    pipeline.discover()
    row = query_one("SELECT a.login FROM repo r JOIN account a ON a.id = r.account_id "
                    " WHERE r.full_name = 'alpha/one'")
    assert row and row["login"] == "alpha"


def test_the_shrink_guard_stands_down_when_an_account_errored(two_accounts, db, monkeypatch):
    """A collapse already explained by a reported failure is not evidence of a
    bad credential, and refusing the run twice for one cause helps nobody."""
    monkeypatch.setattr(pipeline, "GitHubClient", _client_returning(
        {"alpha": RuntimeError("down"), "beta": [_record("beta/still-here")]}))
    monkeypatch.setattr(pipeline, "select_repos", lambda records, cfg: list(records))

    selected = pipeline.discover()          # must not raise the shrink AuthError
    assert [r.full_name for r in selected] == ["beta/still-here"]
