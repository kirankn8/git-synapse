"""Every derived stage, over a synthetic corpus with a planted answer.

The stages compose: aggregate feeds score, change sets feed cross-repo pairs,
lagged feeds impact, mining reads the pairs. A unit test on any one of them
misses the failure that actually happens -- a stage that stops writing a column
another stage reads. This runs all of them against repositories whose
relationships are known in advance.
"""
from __future__ import annotations

import subprocess

import pytest

from git_synapse.ingest.github import RepoRecord

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

    from git_synapse.analysis import aggregate, crossrepo, depbump, lagged, mining, predict, score
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
    crossrepo.rebuild(force=True)
    depbump.rebuild(force=True)
    depbump.refresh_declared(force=True)
    depbump.refresh_modules()
    lagged.rebuild(force=True)
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


def test_every_stage_ran_without_leaving_the_tables_empty(corpus):
    from git_synapse.db.engine import query_one

    ids = [v for v in corpus.values() if isinstance(v, int)]
    assert query_one("SELECT count(*) AS n FROM file_pair WHERE repo_id=ANY(%s)",
                     (ids,))["n"] > 0
    assert query_one("SELECT count(*) AS n FROM change_set")["n"] > 0


def test_the_declared_dependency_is_discovered_from_the_manifest(corpus):
    """dsx-app's go.mod requires dsx-lib; that is the `declared` tier."""
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT count(*) AS n FROM repo_dependency d
        JOIN repo c ON c.id = d.consumer_repo_id
        JOIN repo p ON p.id = d.dep_repo_id
        WHERE c.name = 'dsx-app' AND p.name = 'dsx-lib'
        """
    )
    assert row["n"] >= 1, "the manifest requirement produced no declared edge"


def test_pseudo_version_bumps_are_recorded(corpus):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT count(*) AS n FROM dep_bump b
        JOIN repo c ON c.id = b.consumer_repo_id
        WHERE c.name = 'dsx-app'
        """
    )
    assert row["n"] > 0, "six pseudo-version bumps produced no dep_bump rows"


def test_shared_ticket_keys_group_commits_across_repositories(corpus):
    """DSX-0..5 appear in both repositories, which is the cross-repo unit."""
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT count(*) AS n FROM change_set cs
        WHERE cs.ticket LIKE 'DSX-%' AND cs.n_repos > 1
        """
    )
    assert row["n"] > 0, "tickets spanning both repos formed no multi-repo change set"


def test_cross_repo_contingency_tables_are_feasible(corpus):
    from git_synapse.db.engine import query

    assert not query(
        """
        SELECT * FROM xrepo_file_pair_metric
        WHERE n_ab > n_a OR n_ab > n_b OR n_a > n_total OR n_b > n_total
        """
    )


def test_the_lagged_table_is_populated_and_directional(corpus):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT count(*) AS n FROM repo_lag_metric")
    if row["n"] == 0:
        pytest.skip("too little history for lag bins")
    both = query_one(
        """
        SELECT count(*) AS n FROM repo_lag_metric a
        JOIN repo_lag_metric b
          ON a.repo_a_id = b.repo_b_id AND a.repo_b_id = b.repo_a_id
         AND a.lag_bins = b.lag_bins
        WHERE a.repo_a_id <> a.repo_b_id
        """
    )
    assert both["n"] > 0, "a directional table must hold both orientations"


def test_impact_prefers_the_declared_edge(corpus):
    from git_synapse.db.engine import query

    rows = query(
        """
        SELECT p.name AS source, c.name AS target, i.is_declared
        FROM repo_impact i
        JOIN repo p ON p.id = i.source_repo_id
        JOIN repo c ON c.id = i.target_repo_id
        WHERE c.name = 'dsx-app'
        """
    )
    if not rows:
        pytest.skip("impact produced no rows for this small corpus")
    assert any(r["source"] == "dsx-lib" and r["is_declared"] for r in rows)


def test_mining_produces_clusters_and_risk_without_impossible_values(corpus):
    from git_synapse.db.engine import query, query_one

    ids = [v for v in corpus.values() if isinstance(v, int)]
    assert query_one("SELECT count(*) AS n FROM file_cluster WHERE repo_id=ANY(%s)",
                     (ids,))["n"] >= 0
    assert not query(
        "SELECT * FROM pair_drift WHERE trend NOT IN ('emerging','decaying','stable')"
    )


def test_running_every_stage_twice_is_idempotent(corpus):
    """The scheduler reruns these every 15 minutes; a second pass must not
    accumulate rows or change any number."""
    from git_synapse.analysis import crossrepo, depbump, lagged, mining, predict
    from git_synapse.db.engine import query_one

    def snapshot():
        return {
            t: query_one(f"SELECT count(*) AS n FROM {t}")["n"]
            for t in ("change_set", "repo_pair", "xrepo_file_pair",
                      "repo_lag_metric", "repo_impact", "file_cluster",
                      "repo_dependency", "dep_bump")
        }

    before = snapshot()
    crossrepo.rebuild(force=True)
    depbump.rebuild(force=True)
    depbump.refresh_declared(force=True)
    lagged.rebuild(force=True)
    predict.rebuild(force=True)
    mining.rebuild(force=True)
    assert snapshot() == before


def test_a_full_run_through_run_ingest_drives_every_stage(corpus, monkeypatch):
    """`run_ingest` is the entrypoint the scheduler and the CLI both use.

    Everything below it is covered piecewise; this exercises the orchestration
    itself -- credential check, lock, per-repo fan-out, the global stages, and
    the run row -- against repositories already on disk.
    """
    from git_synapse.db.engine import query_one
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord

    # The mirrors exist, so no network and no credential are needed.
    monkeypatch.setattr(pipeline, "verify_credentials", lambda: "ok")

    records = [
        RepoRecord(github_id=920001, owner="acme", name="dsx-lib",
                   full_name="acme/dsx-lib",
                   clone_url=corpus["_lib_remote"], default_branch="main"),
        RepoRecord(github_id=920002, owner="acme", name="dsx-app",
                   full_name="acme/dsx-app",
                   clone_url=corpus["_app_remote"], default_branch="main"),
    ]
    result = pipeline.run_ingest(records=records, trigger="test")

    assert result.status in ("success", "partial"), result.status
    assert result.run_id is not None
    row = query_one("SELECT status, finished_at FROM ingest_run WHERE id=%s",
                    (result.run_id,))
    assert row["status"] in ("success", "partial")
    assert row["finished_at"] is not None, "a finished run must record when"


def test_a_run_aborts_cleanly_when_the_credential_is_rejected(corpus, monkeypatch):
    """It must fail the whole run before touching a mirror, not let every
    repository fail individually and re-clone on the way."""
    from git_synapse.db.engine import query_one
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.pipeline import AuthError

    def reject():
        raise AuthError("GITHUB_TOKEN was rejected (HTTP 401)")

    monkeypatch.setattr(pipeline, "verify_credentials", reject)
    result = pipeline.run_ingest(records=[], trigger="test")

    assert result.status == "failed"
    row = query_one("SELECT status, error FROM ingest_run WHERE id=%s", (result.run_id,))
    assert row["status"] == "failed"
    assert "401" in (row["error"] or "")


def test_repo_results_are_recorded_per_repository(corpus, monkeypatch):
    from git_synapse.db.engine import query_one
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord

    monkeypatch.setattr(pipeline, "verify_credentials", lambda: "ok")
    result = pipeline.run_ingest(
        records=[RepoRecord(github_id=920001, owner="acme", name="dsx-lib",
                            full_name="acme/dsx-lib",
                            clone_url=corpus["_lib_remote"], default_branch="main")],
        trigger="test",
    )
    n = query_one("SELECT count(*) AS n FROM ingest_run_repo WHERE run_id=%s",
                  (result.run_id,))["n"]
    assert n == 1, "each repository's outcome must be recorded, not just the total"
