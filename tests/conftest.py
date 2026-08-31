"""Shared fixtures. Integration tests skip cleanly when no database is present."""

from __future__ import annotations

import os

import pytest

# Point the integration tests at the compose stack's published port unless the
# environment already says otherwise.
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "55432")


@pytest.fixture(scope="module")
def scratch_db(db):
    """A throwaway database for tests that write.

    Module-scoped, not session-scoped: switching ``POSTGRES_DB`` is process-wide,
    so a session-scoped switch would point the tests that read the real corpus at
    an empty database for the rest of the run.

    Tests that create fixture repositories or call a global ``rebuild()`` used to
    run against whatever ``POSTGRES_DB`` pointed at, which in practice was the
    production corpus: those rebuilds TRUNCATE shared tables, so running the
    suite silently replaced real analysis results and left orphan rows behind.
    """
    import psycopg

    from git_synapse.config import get_config, reset_config_cache
    from git_synapse.db.engine import apply_schema, close_pool

    cfg = get_config().db
    name = f"{cfg.database}_test"
    maintenance = (
        f"host={cfg.host} port={cfg.port} user={cfg.user} "
        f"password={cfg.password} dbname=postgres"
    )
    with psycopg.connect(maintenance, autocommit=True) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone():
            conn.execute(f'CREATE DATABASE "{name}"')

    previous = os.environ.get("POSTGRES_DB")
    os.environ["POSTGRES_DB"] = name
    reset_config_cache()
    close_pool()
    apply_schema()
    try:
        yield name
    finally:
        close_pool()
        if previous is None:
            os.environ.pop("POSTGRES_DB", None)
        else:
            os.environ["POSTGRES_DB"] = previous
        reset_config_cache()


@pytest.fixture(scope="session")
def db():
    """Yield a working database, or skip the test when none is reachable."""
    from git_synapse.db.engine import apply_schema, wait_for_database

    try:
        wait_for_database(timeout_s=5, interval_s=0.5)
    except Exception as exc:  # noqa: BLE001 - any failure means "no database"
        pytest.skip(f"no database available: {exc}")
    apply_schema()
    return True


# --------------------------------------------------------------- corpus
# A real, tiny corpus: two git repositories, ingested and put through every
# derived stage. Tests that need data used to read whatever the developer's
# database happened to hold, which meant they failed outright on a fresh one
# instead of testing anything.

import subprocess  # noqa: E402

from git_synapse.ingest.github import RepoRecord  # noqa: E402

ENV = {
    "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _remote(root, name, commits):
    work = root / f"{name}-w"
    work.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    for subject, files in commits:
        for rel, body in files.items():
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
        subprocess.run(["git", "commit", "--quiet", "-m", subject],
                       cwd=work, check=True, env=ENV)
    bare = root / f"{name}.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)
    return bare


@pytest.fixture(scope="module")
def corpus(scratch_db, tmp_path_factory):
    """Two repositories linked by a declared dependency and shared tickets."""
    import os

    from git_synapse.config import reset_config_cache

    root = tmp_path_factory.mktemp("derived")
    previous = os.environ.get("MIRROR_ROOT")
    os.environ["MIRROR_ROOT"] = str(root / "mirrors")
    reset_config_cache()

    from git_synapse.analysis import aggregate, depbump, mining, predict, score
    from git_synapse.db.engine import connection
    from git_synapse.ingest import pipeline

    # `dsx-lib` is the upstream; `dsx-app` declares it in go.mod and bumps it.
    lib = _remote(root, "dsx-lib", [
        (f"DSX-{i} lib change {i}", {"pkg/core.go": f"package core // {i}",
                                     "pkg/core_test.go": f"package core // t{i}"})
        for i in range(6)
    ])
    app_commits = []
    for i in range(6):
        app_commits.append((
            f"DSX-{i} app change {i}",
            {
                "go.mod": (
                    "module github.com/acme/dsx-app\n\n"
                    "require github.com/acme/dsx-lib "
                    f"v0.0.0-2026010100000{i}-abcdef01234{i}\n"
                ),
                "cmd/main.go": f"package main // {i}",
            },
        ))
    app = _remote(root, "dsx-app", app_commits)

    records = [
        RepoRecord(github_id=920001, owner="acme", name="dsx-lib",
                   full_name="acme/dsx-lib", clone_url=str(lib),
                   default_branch="main"),
        RepoRecord(github_id=920002, owner="acme", name="dsx-app",
                   full_name="acme/dsx-app", clone_url=str(app),
                   default_branch="main"),
    ]
    for r in records:
        res = pipeline.sync_repo(r, force_full=True)
        assert res.status != "failed", res.error

    with connection() as conn:
        ids = {r[1]: r[0] for r in conn.execute(
            "SELECT id, name FROM repo WHERE github_id = ANY(%s)", ([920001, 920002],)
        ).fetchall()}
        for rid in ids.values():
            aggregate.rebuild_repo(rid, conn)
    with connection() as conn:
        for rid in ids.values():
            score.score_repo(rid, conn)

    # Every global stage, in the order the pipeline runs them.
    depbump.rebuild(force=True)
    depbump.refresh_declared(force=True)
    depbump.refresh_modules()
    predict.rebuild(force=True)
    mining.rebuild(force=True)

    ids["_lib_remote"] = str(lib)
    ids["_app_remote"] = str(app)
    try:
        yield ids
    finally:
        if previous is None:
            os.environ.pop("MIRROR_ROOT", None)
        else:
            os.environ["MIRROR_ROOT"] = previous
        reset_config_cache()


@pytest.fixture
def settled_calls():
    """Wait until queued calls have reached the database.

    Recording is asynchronous by design: a caller hands a row to a queue and a
    daemon thread writes it. A test that flushes by hand races that thread and
    finds an empty queue, so it waits for the row instead of assuming which of
    the two got there first.
    """
    import time

    from git_synapse.analysis import calls

    def wait(predicate, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            calls._flush_once()
            found = predicate()
            if found:
                return found
            time.sleep(0.05)
        raise AssertionError("queued calls never reached the database")

    return wait
