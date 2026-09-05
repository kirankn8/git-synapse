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


# ------------------------------------------------------------ lagged internals


# ------------------------------------------------------------ predict internals

def test_the_input_fingerprint_changes_only_when_the_inputs_do(db):
    """It is what lets the fifteen-minute tick skip a rebuild; if it moved on
    its own the corpus would be rebuilt every time for nothing."""
    from git_synapse.analysis.predict import _input_fingerprint
    from git_synapse.db.engine import connection

    with connection() as conn:
        first = _input_fingerprint(conn)
        second = _input_fingerprint(conn)
    assert first == second


# --------------------------------------------------------- the MCP entrypoint

def test_the_mcp_entrypoint_defaults_to_stdio(monkeypatch):
    """`main()` is how the container starts. If argument parsing is wrong the
    server never comes up and the only symptom is tools that never register."""
    from git_synapse.mcp import server

    started = {}
    monkeypatch.setattr(server.server, "run", lambda **kw: started.update(kw) or 0)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)

    assert server.main([]) == 0
    assert started.get("transport") == "stdio"


def test_the_mcp_entrypoint_accepts_http_with_host_and_port(monkeypatch):
    """http does not go through `server.run`: the app is wrapped in a token
    check first, which means serving it ourselves. Patch what actually serves,
    or the test starts a real server and the suite hangs rather than fails."""
    from git_synapse.mcp import server

    served = {}
    monkeypatch.setattr(server, "_serve",
                        lambda app, host, port: served.update(app=app, host=host, port=port))

    assert server.main(["--transport", "http", "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert served["host"] == "0.0.0.0" and served["port"] == 9999
    assert served["app"] is not None, "the guarded app, not the bare one"


def test_the_mcp_transport_can_come_from_the_environment(monkeypatch):
    """The container sets MCP_TRANSPORT rather than passing flags.

    http serves its own wrapped app, so `_serve` is what to patch here. With
    only `server.run` patched this reached the real uvicorn and the suite hung
    at 46% -- no failure, no output, just a process waiting to be killed.
    """
    from git_synapse.mcp import server

    served = {}
    monkeypatch.setattr(server, "_serve",
                        lambda app, host, port: served.update(app=app, host=host, port=port))
    monkeypatch.setenv("MCP_TRANSPORT", "http")

    assert server.main([]) == 0
    assert served["app"] is not None and served["port"] == 8081


# ------------------------------------------------------------------ asymmetry


# --------------------------------------------------------- run status wording

def test_a_run_with_some_failures_is_partial_not_success(db, monkeypatch):
    """"success" on a run where a quarter of the corpus failed is the kind of
    green that stops anyone looking."""
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import RepoResult

    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")
    monkeypatch.setattr(pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)

    def half_fail(record, force_full=False):
        ok = record.name.endswith("0")
        return RepoResult(full_name=record.full_name,
                          status="success" if ok else "failed",
                          error=None if ok else "something specific")

    monkeypatch.setattr(pipeline, "sync_repo", half_fail)
    records = [
        RepoRecord(github_id=i, owner="t", name=f"p{i}", full_name=f"t/p{i}",
                   clone_url="", default_branch="main")
        for i in range(4)
    ]
    result = pipeline.run_ingest(records=records, trigger="test")
    assert result.status == "partial", result.status


def test_a_run_where_everything_succeeds_is_success(db, monkeypatch):
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.pipeline import RepoResult

    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")
    monkeypatch.setattr(pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)
    monkeypatch.setattr(
        pipeline, "sync_repo",
        lambda record, force_full=False: RepoResult(full_name=record.full_name, status="success"),
    )
    records = [
        RepoRecord(github_id=i, owner="t", name=f"q{i}", full_name=f"t/q{i}",
                   clone_url="", default_branch="main")
        for i in range(3)
    ]
    assert pipeline.run_ingest(records=records, trigger="test").status == "success"


def test_run_ingest_discovers_when_given_no_records(db, monkeypatch):
    from git_synapse.ingest import pipeline

    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")
    monkeypatch.setattr(pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)
    called = []
    monkeypatch.setattr(pipeline, "discover", lambda trigger: called.append(trigger) or [])

    pipeline.run_ingest(records=None, trigger="test")
    assert called == ["test"], "no records means discover, not do nothing"


# ------------------------------------------------------------------ health

def test_health_never_throws_even_when_the_database_is_unreachable(monkeypatch):
    """The health endpoint is what a supervisor polls; it must report a problem
    rather than become one."""
    import git_synapse.api.routes as routes

    def broken(*a, **kw):
        raise RuntimeError("database gone")

    monkeypatch.setattr(routes.q, "overview", broken, raising=False)
    body = routes.health()
    assert isinstance(body, dict)
    assert body.get("database") not in (None, "ok")
