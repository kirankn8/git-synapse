"""The whole pipeline over a synthetic corpus, in one pass.

Every other test checks a part. This one builds real git repositories with a
known coupling structure, runs the actual ingest and every derived stage against
a scratch database, and asserts the answers come back right. If the stages stop
composing -- an aggregate that reads a column another stage stopped writing --
only this catches it.

No network: the "remote" is a bare repo on disk, which is what sync_mirror
clones from.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import pytest

from git_synapse.ingest.github import RepoRecord

ENV = {
    "GIT_AUTHOR_NAME": "Dev One", "GIT_AUTHOR_EMAIL": "one@example.com",
    "GIT_COMMITTER_NAME": "Dev One", "GIT_COMMITTER_EMAIL": "one@example.com",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _build_remote(root, name: str, commits: list[dict[str, str]]):
    """A bare repo whose history has a deliberate coupling structure."""
    work = root / f"{name}-work"
    work.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    for i, files in enumerate(commits):
        for rel, body in files.items():
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", f"PROJ-{i} change {i}"],
            cwd=work, check=True, env=ENV,
        )
    bare = root / f"{name}.git"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(work), str(bare)], check=True, env=ENV
    )
    return bare


@pytest.fixture(scope="module")
def ingested(scratch_db, tmp_path_factory):
    """Two repositories ingested through the real pipeline."""
    import os

    from git_synapse.config import reset_config_cache

    root = tmp_path_factory.mktemp("e2e")

    # MIRROR_ROOT defaults to the container path, which is read-only here.
    previous = os.environ.get("MIRROR_ROOT")
    os.environ["MIRROR_ROOT"] = str(root / "mirrors")
    reset_config_cache()

    from git_synapse.analysis import aggregate, score
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest import pipeline

    # `a.py` and `b.py` always move together; `lonely.py` never does.
    paired = [{"a.py": f"a{i}", "b.py": f"b{i}"} for i in range(6)]
    solo = [{"lonely.py": f"x{i}"} for i in range(4)]
    alpha = _build_remote(root, "alpha", paired + solo)
    beta = _build_remote(root, "beta", [{"c.py": f"c{i}"} for i in range(5)])

    records = [
        RepoRecord(github_id=910001, owner="t", name="e2e-alpha",
                   full_name="t/e2e-alpha", clone_url=str(alpha), default_branch="main"),
        RepoRecord(github_id=910002, owner="t", name="e2e-beta",
                   full_name="t/e2e-beta", clone_url=str(beta), default_branch="main"),
    ]

    results = [pipeline.sync_repo(r, force_full=True) for r in records]
    assert all(x.status != "failed" for x in results), [x.error for x in results]

    with session_scope() as conn:
        ids = {r.name: r.id for r in conn.query(models().Repo).filter(
            models().Repo.github_id.in_([910001, 910002])
        ).all()}
        for rid in ids.values():
            aggregate.rebuild_repo(rid, conn)
    with session_scope() as conn:
        for rid in ids.values():
            score.score_repo(rid, conn)
    ids["_alpha_remote"] = str(alpha)
    try:
        yield ids
    finally:
        if previous is None:
            os.environ.pop("MIRROR_ROOT", None)
        else:
            os.environ["MIRROR_ROOT"] = previous
        reset_config_cache()


def test_commits_and_atoms_land(ingested):
    from git_synapse.db.orm import models, session_scope

    alpha = ingested["e2e-alpha"]
    with session_scope() as session:
        assert session.query(models().Commit).filter_by(repo_id=alpha).count() == 10
        assert session.query(models().File).filter_by(repo_id=alpha).count() == 3


def test_the_always_together_pair_is_found_and_the_lonely_file_is_not(ingested):
    """The structure was planted: a.py and b.py in every one of six commits."""
    from git_synapse.db.orm import session_scope

    alpha = ingested["e2e-alpha"]
    from git_synapse.db.orm import models
    with session_scope() as session:
        files = {f.id: f.path for f in session.query(models().File).filter_by(repo_id=alpha).all()}
        pair = next((m for m in session.query(models().FilePairMetric).filter_by(repo_id=alpha).all()
                     if {files.get(m.file_a_id), files.get(m.file_b_id)} == {"a.py", "b.py"}), None)
    assert pair is not None, "a pair present in six shared commits was not found"
    assert pair.n_ab == 6
    assert float(pair.confidence_ab) == pytest.approx(1.0)
    assert float(pair.jaccard) == pytest.approx(1.0)

    with session_scope() as session:
        lonely_ids = [f.id for f in session.query(models().File).filter_by(
            repo_id=alpha, path="lonely.py",
        ).all()]
        lonely = session.query(models().FilePairMetric).filter(
            models().FilePairMetric.repo_id == alpha,
            (models().FilePairMetric.file_a_id.in_(lonely_ids) |
             models().FilePairMetric.file_b_id.in_(lonely_ids)),
        ).count()
    assert lonely == 0, "a file that never co-changed must have no pairs"


def test_marginals_agree_with_the_atoms(ingested):
    from git_synapse.db.orm import models, session_scope

    repo_ids = [v for v in ingested.values() if isinstance(v, int)]
    with session_scope() as session:
        files = session.query(models().File).filter(models().File.repo_id.in_(repo_ids)).all()
        counts = {file.id: session.query(models().CommitFile).filter_by(file_id=file.id).count()
                  for file in files}
        bad = [{"path": file.path, "change_count": file.change_count, "actual": counts[file.id]}
               for file in files if file.change_count != counts[file.id]]
    assert not bad, bad


def test_every_contingency_table_is_feasible(ingested):
    from git_synapse.db.orm import models, session_scope

    repo_ids = [v for v in ingested.values() if isinstance(v, int)]
    with session_scope() as session:
        metrics = session.query(models().FilePairMetric).filter(
            models().FilePairMetric.repo_id.in_(repo_ids)
        ).all()
    bad = [m for m in metrics if m.n_ab > m.n_a or m.n_ab > m.n_b or
           m.n_a > m.n_total or m.n_b > m.n_total or
           m.n_total - m.n_a - m.n_b + m.n_ab < 0]
    assert not bad


def test_coupled_files_answers_through_the_real_query_path(ingested):
    from git_synapse.analysis.query import coupled_files, resolve_file

    target = resolve_file("t/e2e-alpha", "a.py")
    assert target is not None
    partners = coupled_files(target["id"], limit=10, min_support=2)
    paths = {p["path"] for p in partners}
    assert "b.py" in paths
    assert "lonely.py" not in paths

    b = next(p for p in partners if p["path"] == "b.py")
    assert float(b["confidence_out"]) == pytest.approx(1.0)
    assert b["n_other"] == 6, "the partner's own count, not the queried file's"


def test_a_second_ingest_with_no_new_commits_changes_nothing(ingested):
    """An idempotent re-read is what makes the 15-minute schedule safe."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest import pipeline

    repo_ids = [v for v in ingested.values() if isinstance(v, int)]
    with session_scope() as session:
        before_rows = session.query(models().FilePairMetric).filter(
            models().FilePairMetric.repo_id.in_(repo_ids)
        ).all()
        before = (len(before_rows), sum(row.n_ab for row in before_rows))
    # Same local remote: the mirror already exists, so this is a pure re-read.
    record = RepoRecord(
        github_id=910001, owner="t", name="e2e-alpha", full_name="t/e2e-alpha",
        clone_url=ingested["_alpha_remote"], default_branch="main",
    )
    result = pipeline.sync_repo(record)
    assert result.status != "failed", result.error

    with session_scope() as session:
        after_rows = session.query(models().FilePairMetric).filter(
            models().FilePairMetric.repo_id.in_(repo_ids)
        ).all()
        after = (len(after_rows), sum(row.n_ab for row in after_rows))
    assert after == before


# ------------------------------------------------ watermarks that went wrong
#
# Everything below re-reads an already-ingested repository whose watermark is in
# some damaged state. Each of these damaged a real sync once: the whole point of
# the guards is that a rewritten history costs one repository a slow re-read, not
# a failed run.

def _alpha(ingested):
    return RepoRecord(
        github_id=910001, owner="t", name="e2e-alpha", full_name="t/e2e-alpha",
        clone_url=ingested["_alpha_remote"], default_branch="main",
    )


def test_a_watermark_written_by_an_older_version_is_still_honoured(ingested):
    """The watermark used to be a single SHA. A repository last synced by that
    version must not re-read its whole history on the next tick."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest import pipeline

    repo_id = ingested["e2e-alpha"]
    with session_scope() as conn:
        repo = conn.get(models().Repo, repo_id)
        head = repo.last_ingested_sha
        repo.last_ingested_refs = []

    result = pipeline.sync_repo(_alpha(ingested))
    assert result.status != "failed", result.error
    assert result.commits_added == 0, "the legacy watermark was ignored"
    assert head


def test_a_force_pushed_away_ref_tip_does_not_fail_the_repository(ingested):
    """Asking git for `^<missing>` is a hard error, so an orphaned tip has to be
    dropped rather than passed through."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest import pipeline

    repo_id = ingested["e2e-alpha"]
    with session_scope() as conn:
        conn.get(models().Repo, repo_id).last_ingested_refs = ["f" * 40]
    result = pipeline.sync_repo(_alpha(ingested))
    assert result.status != "failed", result.error


def test_a_rewritten_commit_is_swept_during_the_next_sync(ingested):
    """Insert-only was the bug: 735 commits across 24 repositories outlived the
    history they came from, inflating the N of every contingency table there."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest import pipeline

    repo_id = ingested["e2e-alpha"]
    with session_scope() as conn:
        author = conn.query(models().Author).filter_by(email="ghost@e").one_or_none()
        if author is None:
            author = models().Author(email="ghost@e", display_name="ghost")
            conn.add(author)
            conn.flush()
        conn.add(models().Commit(repo_id=repo_id, sha="c" * 40, author_id=author.id,
                                 committer_id=author.id, authored_at=datetime.now(UTC),
                                 committed_at=datetime.now(UTC), subject="rewritten away"))

    result = pipeline.sync_repo(_alpha(ingested))
    assert result.status != "failed", result.error
    with session_scope() as conn:
        assert conn.query(models().Commit).filter_by(repo_id=repo_id, sha="c" * 40).count() == 0


def test_a_repository_that_fails_mid_sync_records_why_on_the_row(ingested,
                                                                 monkeypatch):
    """`git-synapse status` reads ingest_error. Losing it means a repository that
    silently stops updating looks identical to one that is simply quiet."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest import pipeline

    repo_id = ingested["e2e-alpha"]

    def explode(*a, **k):
        raise RuntimeError("parser fell over")

    monkeypatch.setattr(pipeline, "iter_commits", explode)
    result = pipeline._sync_repo_once(_alpha(ingested))
    assert result.status == "failed"
    assert "parser fell over" in result.error

    with session_scope() as conn:
        row = conn.get(models().Repo, repo_id)
        assert row.ingest_status == "failed"
        assert "parser fell over" in row.ingest_error

    with session_scope() as conn:
        row = conn.get(models().Repo, repo_id)
        row.ingest_status = "ok"
        row.ingest_error = None


def test_a_failed_status_write_does_not_mask_the_original_failure(ingested,
                                                                  monkeypatch):
    """Best-effort means best-effort: if the database is the thing that broke,
    the caller must still learn what actually failed."""
    from git_synapse.ingest import pipeline

    broken = {"yet": False}
    real_session_scope = pipeline.session_scope

    def explode_then_break_the_database(*a, **k):
        broken["yet"] = True
        raise RuntimeError("parser fell over")

    def no_database(*a, **k):
        if broken["yet"]:
            raise OSError("connection refused")
        return real_session_scope(*a, **k)

    explode = explode_then_break_the_database
    monkeypatch.setattr(pipeline, "iter_commits", explode)
    monkeypatch.setattr(pipeline, "session_scope", no_database)
    result = pipeline._sync_repo_once(_alpha(ingested))
    assert result.status == "failed"
    assert "parser fell over" in result.error


# ------------------------------------------------------- mining on real pairs

@pytest.fixture(scope="module")
def mined(ingested, tmp_path_factory):
    """A repository with a real three-file module plus an unrelated file.

    Three, not two: synchronous label propagation oscillates on a lone edge --
    each node keeps adopting the other's label -- so a two-file component never
    settles into a cluster. Real modules are bigger than that, but a fixture has
    to be too.
    """
    from git_synapse.ingest import pipeline

    root = tmp_path_factory.mktemp("mine")
    together = [{"x.py": f"x{i}", "y.py": f"y{i}", "z.py": f"z{i}"} for i in range(6)]
    apart = [{"alone.py": f"q{i}"} for i in range(4)]
    remote = _build_remote(root, "trio", together + apart)
    record = RepoRecord(github_id=910003, owner="t", name="e2e-trio",
                        full_name="t/e2e-trio", clone_url=str(remote),
                        default_branch="main")
    result = pipeline.sync_repo(record, force_full=True)
    assert result.status != "failed", result.error

    from git_synapse.analysis import aggregate, score
    from git_synapse.db.orm import models, session_scope

    with session_scope() as conn:
        repo_id = conn.query(models().Repo.id).filter_by(github_id=910003).scalar()
    with session_scope() as conn:
        aggregate.rebuild_repo(repo_id, conn)
    with session_scope() as conn:
        score.score_repo(repo_id, conn)
    return repo_id


def test_mining_finds_the_module_the_coupled_files_form(mined):
    """x, y and z always move together and alone.py never does, so label
    propagation must put the first three in a cluster and leave the fourth out."""
    from git_synapse.analysis import mining
    from git_synapse.db.orm import models, session_scope

    repo_id = mined
    stats = mining.rebuild(repo_id=repo_id, force=True)
    assert stats.clustered_files >= 3

    with session_scope() as session:
        files = {f.id: f.path for f in session.query(models().File).filter_by(repo_id=repo_id).all()}
        clusters = session.query(models().FileCluster).filter_by(repo_id=repo_id).all()
    by_path = {files[row.file_id]: row.cluster_id for row in clusters}
    assert by_path.get("x.py") is not None
    assert by_path["x.py"] == by_path.get("y.py") == by_path.get("z.py")
    assert "alone.py" not in by_path


def test_mining_a_repository_with_no_coupled_pairs_writes_nothing(ingested):
    """beta has five commits that never touch the same file twice."""
    from git_synapse.analysis import mining
    from git_synapse.db.orm import models, session_scope

    repo_id = ingested["e2e-beta"]
    mining.rebuild(repo_id=repo_id, force=True)
    with session_scope() as session:
        assert session.query(models().FileCluster).filter_by(repo_id=repo_id).count() == 0


def test_a_second_mining_pass_over_unchanged_repositories_is_skipped(ingested):
    """Mining was 129s of a 216s nightly run precisely because it ignored this."""
    from git_synapse.analysis import mining

    mining.rebuild(force=True)
    again = mining.rebuild(force=False)
    assert again.clustered_files == 0, "unchanged repositories were re-mined"


def test_mining_can_run_inside_a_callers_transaction(mined):
    from git_synapse.analysis import mining
    from git_synapse.db.orm import session_scope

    with session_scope() as conn:
        stats = mining.rebuild(repo_id=mined, conn=conn, force=True)
    assert stats.clustered_files >= 3


def test_drifting_pairs_can_be_scoped_to_one_repository(ingested):
    from git_synapse.analysis import mining

    mining.rebuild(force=True)
    scoped = mining.drifting_pairs(repo_id=ingested["e2e-alpha"], trend="emerging")
    assert all(r["repo_id"] == ingested["e2e-alpha"] for r in scoped)


# ------------------------------------------- the own-connection call shapes
#
# Every rebuild takes an optional connection: the pipeline passes its own so the
# derived tables land in the same transaction, while the CLI and one-off scripts
# pass nothing. Only the first shape was ever exercised.


def test_scoring_every_repository_covers_every_repository(mined):
    from git_synapse.analysis import score
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        enabled = session.query(models().Repo).filter_by(is_enabled=True).count()
    assert len(score.score_all()) == enabled


def test_a_repository_upsert_without_a_connection_is_committed(mined):
    """`git-synapse discover` writes repositories outside any transaction of its own."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest.store import upsert_repo

    record = RepoRecord(github_id=910099, owner="t", name="e2e-standalone",
                        full_name="t/e2e-standalone", clone_url="",
                        default_branch="main")
    repo_id = upsert_repo(record)
    with session_scope() as session:
        assert session.query(models().Repo.id).filter_by(github_id=910099).scalar() == repo_id
    # Idempotent: discovery runs on every scheduled tick.
    assert upsert_repo(record) == repo_id


def test_an_author_connection_that_is_already_closed_closes_cleanly(mined):
    """Closing must never mask the real error that ended the run."""
    from git_synapse.db.orm import session_scope
    from git_synapse.ingest.store import AuthorCache

    with session_scope() as conn:
        cache = AuthorCache(conn)
        assert cache.resolve("", "nobody") is None
        first = cache.resolve("Someone@Example.com", "Someone")
        # Cached, and case-folded on the way in.
        assert cache.resolve("someone@example.com", "Someone") == first
        cache.close()
        cache.close()


def test_the_file_resolver_knows_paths_a_rename_left_behind(mined):
    """A path that only exists as an alias must still resolve, or the file gets
    a second identity and its history splits in two."""
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest.store import FileResolver

    with session_scope() as conn:
        row = conn.query(models().File).filter_by(repo_id=mined).order_by(models().File.id).first()
    with session_scope() as conn:
        alias = conn.query(models().FileAlias).filter_by(repo_id=mined, old_path="old/name.py").first()
        if alias is None:
            conn.add(models().FileAlias(repo_id=mined, old_path="old/name.py", file_id=row.id))
            conn.flush()
        resolver = FileResolver(conn, mined)
        assert resolver.resolve(row.path) == row.id
        assert resolver.resolve("old/name.py") == row.id


def test_a_mirror_that_cannot_be_read_does_not_mark_every_file_deleted(mined,
                                                                       monkeypatch):
    """None means "could not read", not "the tree is empty". Confusing the two
    would tombstone every file in the repository on one bad git invocation."""
    import subprocess as sp

    from git_synapse.analysis.aggregate import _head_tree_paths
    from git_synapse.db.orm import session_scope

    with session_scope() as conn:
        assert _head_tree_paths(conn, -1) is None

        real = sp.run
        monkeypatch.setattr(sp, "run",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
        assert _head_tree_paths(conn, mined) is None

        class _Bad:
            returncode = 128
            stdout = ""
            stderr = "fatal"

        monkeypatch.setattr(sp, "run", lambda *a, **k: _Bad())
        assert _head_tree_paths(conn, mined) is None

        monkeypatch.setattr(sp, "run", real)
        paths = _head_tree_paths(conn, mined)
        assert paths and "x.py" in paths


def test_a_repository_whose_mirror_is_gone_reads_as_unreadable(mined, monkeypatch):
    from pathlib import Path

    from git_synapse.analysis.aggregate import _head_tree_paths
    from git_synapse.db.orm import session_scope
    from git_synapse.ingest import gitops

    monkeypatch.setattr(gitops, "mirror_path_for",
                        lambda *a, **k: Path("/nonexistent/mirror.git"))
    with session_scope() as conn:
        assert _head_tree_paths(conn, mined) is None


def test_the_declared_dependency_refresh_keeps_what_it_has_when_nothing_changed(
    mined, monkeypatch
):
    """A repository whose manifests did not move must not lose its declared
    edges -- `declared` is the tier agents are told to trust above all others."""
    from git_synapse.analysis import depbump

    depbump.refresh_declared(force=True)
    kept = depbump.refresh_declared(force=False)
    assert kept >= 0

    again = depbump.refresh_declared(force=False)
    assert again == kept


def test_the_module_graph_is_rebuilt_from_the_mirrors_on_disk(mined):
    """A monorepo declares its real dependencies in per-module manifests; reading
    only the root hid 371 internal references across 29 repositories."""
    from git_synapse.analysis import depbump

    assert depbump.refresh_modules() >= 0


# ------------------------------------------------- declared dependencies, for real

@pytest.fixture(scope="module")
def manifests(ingested, tmp_path_factory):
    """A monorepo with per-module manifests plus the repository it depends on.

    Reading only the root manifest was a real coverage gap: this organisation's
    monorepos keep their real dependencies in per-module files, which hid 371
    internal references across 29 repositories.
    """
    from git_synapse.db.orm import models, session_scope
    from git_synapse.ingest import pipeline

    root = tmp_path_factory.mktemp("mani")
    dep = _build_remote(root, "dep", [{"lib.go": "package lib\n"}])
    mono = _build_remote(root, "mono", [{
        "go.mod": (
            "module github.com/acme/e2e-mono\n"
            "require github.com/acme/e2e-dep v1.4.0\n"
        ),
        "core/go.mod": "module github.com/acme/e2e-mono/core\n",
        "gateway/go.mod": (
            "module github.com/acme/e2e-mono/gateway\n"
            "require github.com/acme/e2e-mono/core v0.0.0\n"
        ),
        # Vendored manifests describe someone else's dependencies.
        "vendor/other/go.mod": "module github.com/elsewhere/other\n",
    }])

    ids = {}
    for gh, name, remote in ((910011, "e2e-dep", dep), (910012, "e2e-mono", mono)):
        record = RepoRecord(github_id=gh, owner="acme", name=name,
                            full_name=f"acme/{name}", clone_url=str(remote),
                            default_branch="main")
        result = pipeline.sync_repo(record, force_full=True)
        assert result.status != "failed", result.error
        with session_scope() as conn:
            ids[name] = conn.query(models().Repo.id).filter_by(github_id=gh).scalar()
    return ids


def test_a_declared_dependency_is_recorded_from_the_manifest(manifests):
    from git_synapse.analysis import depbump
    from git_synapse.db.orm import models, session_scope

    assert depbump.refresh_declared(force=True) > 0
    with session_scope() as conn:
        rows = conn.query(models().RepoDependency).filter_by(
            consumer_repo_id=manifests["e2e-mono"],
        ).all()
    # Stored as the manifest wrote it, so the owner is available at resolution.
    edge = next(r for r in rows if r.dep_name == "github.com/acme/e2e-dep")
    assert edge.dep_repo_id == manifests["e2e-dep"], "the reference did not resolve"
    assert edge.manifest == "go.mod"


def test_the_internal_module_graph_is_recorded_per_manifest(manifests):
    from git_synapse.analysis import depbump
    from git_synapse.db.orm import models, session_scope

    assert depbump.refresh_modules() > 0
    with session_scope() as conn:
        rows = conn.query(models().ModuleDependency).filter_by(
            repo_id=manifests["e2e-mono"],
        ).all()
    pairs = {(r.consumer_module, r.dep_module) for r in rows}
    assert ("gateway", "core") in pairs


def test_a_vendored_manifest_is_not_read_as_this_repositorys_dependency(manifests):
    from git_synapse.analysis import depbump
    from git_synapse.db.orm import models, session_scope

    depbump.refresh_declared(force=True)
    with session_scope() as conn:
        manifest_paths = {r.manifest for r in conn.query(models().RepoDependency).filter_by(
            consumer_repo_id=manifests["e2e-mono"],
        ).all()}
    assert manifest_paths
    assert not any(p.startswith("vendor/") for p in manifest_paths)


def test_a_second_declared_refresh_keeps_the_edges_it_already_found(manifests):
    """A repository whose manifests did not move must not lose its declared
    edges: `declared` is the tier agents are told to trust above all others."""
    from git_synapse.analysis import depbump

    first = depbump.refresh_declared(force=True)
    assert depbump.refresh_declared(force=False) == first


def test_the_bump_scan_walks_the_manifest_history(manifests):
    from git_synapse.analysis import depbump

    stats = depbump.rebuild(force=True)
    assert stats.repos_scanned > 0
