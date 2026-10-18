"""One rule for paths that describe a build rather than a relationship."""
from __future__ import annotations

from git_synapse.analysis.noise import is_noise


def test_manifests_lockfiles_and_generated_trees_are_noise():
    for path in ("go.mod", "svc/package.json", "package-lock.json", "Cargo.lock",
                 "vendor/x/y.go", "node_modules/a/b.js", "api/foo_gen.go",
                 "api/responses.pb.go", "web/app.min.js", "svc/thing_auto_test.go"):
        assert is_noise(path), path


def test_source_files_are_not_noise():
    for path in ("pkg/server/server.go", "ui/src/App.tsx", "cmd/main.go",
                 "docs/readme.md", "deploy/chart/values.yaml", "testdata_fixtures.txt"):
        assert not is_noise(path), path


def test_hotspots_rank_source_files_over_lockfiles(db):
    from uuid import uuid4

    from git_synapse.analysis import query
    from git_synapse.db.orm import models, session_scope

    tag = uuid4().hex[:8]
    with session_scope() as session:
        repo = models().Repo(full_name=f"t/hot-{tag}", name=f"hot-{tag}", owner="t")
        session.add(repo)
        session.flush()
        session.add_all([
            models().File(repo_id=repo.id, path="package-lock.json", basename="package-lock.json",
                          change_count=500),
            models().File(repo_id=repo.id, path="svc/a.go", basename="a.go", change_count=5),
        ])
        repo_id = repo.id

    try:
        rows = query.hotspots(repo_id, limit=5)
        assert [row["path"] for row in rows] == ["svc/a.go"]
    finally:
        with session_scope() as session:
            session.query(models().File).filter_by(repo_id=repo_id).delete(synchronize_session=False)
            session.query(models().Repo).filter_by(id=repo_id).delete(synchronize_session=False)


def test_directory_coupling_never_offers_the_repository_root(monkeypatch):
    from git_synapse.analysis import query

    monkeypatch.setattr(query, "_pair_rows", lambda *a, **kw: [
        {"path": "", "score": 0.9, "co_changes": 900},
        {"path": "gateway", "score": 0.4, "co_changes": 12},
    ])
    rows = query.coupled_directories(1, "npmi", 5)
    assert [row["path"] for row in rows] == ["gateway"]
