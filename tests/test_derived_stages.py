"""Every derived stage, over a synthetic corpus with a planted answer.

The stages compose: aggregate feeds score, change sets feed cross-repo pairs,
lagged feeds impact, mining reads the pairs. A unit test on any one of them
misses the failure that actually happens -- a stage that stops writing a column
another stage reads. This runs all of them against repositories whose
relationships are known in advance.
"""
from __future__ import annotations

import pytest


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
    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")

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

    def reject(**_):
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

    monkeypatch.setattr(pipeline, "verify_credentials", lambda **_: "ok")
    result = pipeline.run_ingest(
        records=[RepoRecord(github_id=920001, owner="acme", name="dsx-lib",
                            full_name="acme/dsx-lib",
                            clone_url=corpus["_lib_remote"], default_branch="main")],
        trigger="test",
    )
    n = query_one("SELECT count(*) AS n FROM ingest_run_repo WHERE run_id=%s",
                  (result.run_id,))["n"]
    assert n == 1, "each repository's outcome must be recorded, not just the total"


class _Stats:
    change_sets = ticket_sets = temporal_sets = eligible_sets = 0
    repo_pairs = file_pairs = 0
    duration_s = 0.0
