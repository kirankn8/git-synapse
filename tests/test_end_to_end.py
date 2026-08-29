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

import json
import subprocess

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

    from git_synapse.analysis import aggregate, crossrepo, score
    from git_synapse.db.engine import connection
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

    with connection() as conn:
        ids = {
            r[1]: r[0] for r in conn.execute(
                "SELECT id, name FROM repo WHERE github_id = ANY(%s)",
                ([910001, 910002],),
            ).fetchall()
        }
        for rid in ids.values():
            aggregate.rebuild_repo(rid, conn)
    with connection() as conn:
        for rid in ids.values():
            score.score_repo(rid, conn)
    crossrepo.rebuild(force=True)
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
    from git_synapse.db.engine import query_one

    alpha = ingested["e2e-alpha"]
    assert query_one("SELECT count(*) AS n FROM commit WHERE repo_id=%s", (alpha,))["n"] == 10
    assert query_one(
        "SELECT count(*) AS n FROM file WHERE repo_id=%s", (alpha,)
    )["n"] == 3


def test_the_always_together_pair_is_found_and_the_lonely_file_is_not(ingested):
    """The structure was planted: a.py and b.py in every one of six commits."""
    from git_synapse.db.engine import query_one

    alpha = ingested["e2e-alpha"]
    pair = query_one(
        """
        SELECT m.n_ab, m.n_a, m.n_b, m.confidence_ab, m.confidence_ba, m.jaccard
        FROM file_pair_metric m
        JOIN file fa ON fa.id = m.file_a_id
        JOIN file fb ON fb.id = m.file_b_id
        WHERE m.repo_id = %s
          AND ((fa.path='a.py' AND fb.path='b.py') OR (fa.path='b.py' AND fb.path='a.py'))
        """,
        (alpha,),
    )
    assert pair is not None, "a pair present in six shared commits was not found"
    assert pair["n_ab"] == 6
    assert float(pair["confidence_ab"]) == pytest.approx(1.0)
    assert float(pair["jaccard"]) == pytest.approx(1.0)

    lonely = query_one(
        """
        SELECT count(*) AS n FROM file_pair_metric m
        JOIN file f ON f.id IN (m.file_a_id, m.file_b_id)
        WHERE m.repo_id = %s AND f.path = 'lonely.py'
        """,
        (alpha,),
    )
    assert lonely["n"] == 0, "a file that never co-changed must have no pairs"


def test_marginals_agree_with_the_atoms(ingested):
    from git_synapse.db.engine import query

    bad = query(
        """
        SELECT f.path, f.change_count, count(cf.commit_id) AS actual
        FROM file f LEFT JOIN commit_file cf ON cf.file_id = f.id
        WHERE f.repo_id = ANY(%s)
        GROUP BY f.id, f.path, f.change_count
        HAVING f.change_count <> count(cf.commit_id)
        """,
        ([v for v in ingested.values() if isinstance(v, int)],),
    )
    assert not bad, bad


def test_every_contingency_table_is_feasible(ingested):
    from git_synapse.db.engine import query

    assert not query(
        """
        SELECT * FROM file_pair_metric
        WHERE repo_id = ANY(%s)
          AND (n_ab > n_a OR n_ab > n_b OR n_a > n_total OR n_b > n_total
               OR n_total - n_a - n_b + n_ab < 0)
        """,
        ([v for v in ingested.values() if isinstance(v, int)],),
    )


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
    from git_synapse.db.engine import query_one
    from git_synapse.ingest import pipeline

    before = query_one(
        "SELECT count(*) AS c, sum(n_ab) AS s FROM file_pair_metric WHERE repo_id=ANY(%s)",
        ([v for v in ingested.values() if isinstance(v, int)],),
    )
    # Same local remote: the mirror already exists, so this is a pure re-read.
    record = RepoRecord(
        github_id=910001, owner="t", name="e2e-alpha", full_name="t/e2e-alpha",
        clone_url=ingested["_alpha_remote"], default_branch="main",
    )
    result = pipeline.sync_repo(record)
    assert result.status != "failed", result.error

    after = query_one(
        "SELECT count(*) AS c, sum(n_ab) AS s FROM file_pair_metric WHERE repo_id=ANY(%s)",
        ([v for v in ingested.values() if isinstance(v, int)],),
    )
    assert (after["c"], after["s"]) == (before["c"], before["s"])


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
    from git_synapse.db.engine import connection, query_one
    from git_synapse.ingest import pipeline

    repo_id = ingested["e2e-alpha"]
    head = query_one("SELECT last_ingested_sha AS s FROM repo WHERE id=%s",
                     (repo_id,))["s"]
    with connection() as conn:
        conn.execute("UPDATE repo SET last_ingested_refs = '[]'::jsonb"
                     " WHERE id = %s", (repo_id,))

    result = pipeline.sync_repo(_alpha(ingested))
    assert result.status != "failed", result.error
    assert result.commits_added == 0, "the legacy watermark was ignored"
    assert head


def test_a_force_pushed_away_ref_tip_does_not_fail_the_repository(ingested):
    """Asking git for `^<missing>` is a hard error, so an orphaned tip has to be
    dropped rather than passed through."""
    from git_synapse.db.engine import connection
    from git_synapse.ingest import pipeline

    repo_id = ingested["e2e-alpha"]
    with connection() as conn:
        conn.execute(
            "UPDATE repo SET last_ingested_refs = %s::jsonb WHERE id = %s",
            (json.dumps(["f" * 40]), repo_id),
        )
    result = pipeline.sync_repo(_alpha(ingested))
    assert result.status != "failed", result.error


def test_a_rewritten_commit_is_swept_during_the_next_sync(ingested):
    """Insert-only was the bug: 735 commits across 24 repositories outlived the
    history they came from, inflating the N of every contingency table there."""
    from git_synapse.db.engine import connection, query_one
    from git_synapse.ingest import pipeline

    repo_id = ingested["e2e-alpha"]
    with connection() as conn:
        author = conn.execute(
            "INSERT INTO author (email, display_name) VALUES ('ghost@e','ghost')"
            " ON CONFLICT (email) DO UPDATE SET display_name='ghost' RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO commit (repo_id, sha, author_id, committer_id,"
            " authored_at, committed_at, subject)"
            " VALUES (%s,%s,%s,%s,now(),now(),'rewritten away')",
            (repo_id, "c" * 40, author, author),
        )

    result = pipeline.sync_repo(_alpha(ingested))
    assert result.status != "failed", result.error
    assert query_one(
        "SELECT count(*) AS n FROM commit WHERE repo_id=%s AND sha=%s",
        (repo_id, "c" * 40),
    )["n"] == 0


def test_a_repository_that_fails_mid_sync_records_why_on_the_row(ingested,
                                                                 monkeypatch):
    """`git-synapse status` reads ingest_error. Losing it means a repository that
    silently stops updating looks identical to one that is simply quiet."""
    from git_synapse.db.engine import connection, query_one
    from git_synapse.ingest import pipeline

    repo_id = ingested["e2e-alpha"]

    def explode(*a, **k):
        raise RuntimeError("parser fell over")

    monkeypatch.setattr(pipeline, "iter_commits", explode)
    result = pipeline._sync_repo_once(_alpha(ingested))
    assert result.status == "failed"
    assert "parser fell over" in result.error

    row = query_one("SELECT ingest_status AS s, ingest_error AS e FROM repo"
                    " WHERE id = %s", (repo_id,))
    assert row["s"] == "failed"
    assert "parser fell over" in row["e"]

    with connection() as conn:
        conn.execute("UPDATE repo SET ingest_status='ok', ingest_error=NULL"
                     " WHERE id=%s", (repo_id,))


def test_a_failed_status_write_does_not_mask_the_original_failure(ingested,
                                                                  monkeypatch):
    """Best-effort means best-effort: if the database is the thing that broke,
    the caller must still learn what actually failed."""
    from git_synapse.ingest import pipeline

    broken = {"yet": False}
    real_connection = pipeline.connection

    def explode_then_break_the_database(*a, **k):
        broken["yet"] = True
        raise RuntimeError("parser fell over")

    def no_database(*a, **k):
        if broken["yet"]:
            raise OSError("connection refused")
        return real_connection(*a, **k)

    explode = explode_then_break_the_database
    monkeypatch.setattr(pipeline, "iter_commits", explode)
    monkeypatch.setattr(pipeline, "connection", no_database)
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
    from git_synapse.db.engine import connection, query_one

    repo_id = query_one("SELECT id FROM repo WHERE github_id = 910003")["id"]
    with connection() as conn:
        aggregate.rebuild_repo(repo_id, conn)
    with connection() as conn:
        score.score_repo(repo_id, conn)
    return repo_id


def test_mining_finds_the_module_the_coupled_files_form(mined):
    """x, y and z always move together and alone.py never does, so label
    propagation must put the first three in a cluster and leave the fourth out."""
    from git_synapse.analysis import mining
    from git_synapse.db.engine import query

    repo_id = mined
    stats = mining.rebuild(repo_id=repo_id, force=True)
    assert stats.clustered_files >= 3

    rows = query(
        "SELECT f.path, c.cluster_id FROM file_cluster c"
        " JOIN file f ON f.id = c.file_id WHERE c.repo_id = %s",
        (repo_id,),
    )
    by_path = {r["path"]: r["cluster_id"] for r in rows}
    assert by_path.get("x.py") is not None
    assert by_path["x.py"] == by_path.get("y.py") == by_path.get("z.py")
    assert "alone.py" not in by_path


def test_mining_a_repository_with_no_coupled_pairs_writes_nothing(ingested):
    """beta has five commits that never touch the same file twice."""
    from git_synapse.analysis import mining
    from git_synapse.db.engine import query_one

    repo_id = ingested["e2e-beta"]
    mining.rebuild(repo_id=repo_id, force=True)
    assert query_one("SELECT count(*) AS n FROM file_cluster WHERE repo_id=%s",
                     (repo_id,))["n"] == 0


def test_a_second_mining_pass_over_unchanged_repositories_is_skipped(ingested):
    """Mining was 129s of a 216s nightly run precisely because it ignored this."""
    from git_synapse.analysis import mining

    mining.rebuild(force=True)
    again = mining.rebuild(force=False)
    assert again.clustered_files == 0, "unchanged repositories were re-mined"


def test_mining_can_run_inside_a_callers_transaction(mined):
    from git_synapse.analysis import mining
    from git_synapse.db.engine import connection

    with connection() as conn:
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

def test_the_rebuilds_all_work_without_a_connection_handed_to_them(mined):
    from git_synapse.analysis import aggregate, crossrepo, depbump, lagged, score

    assert isinstance(aggregate.repos_needing_aggregation(), list)
    assert isinstance(score.score_all(), list)
    assert crossrepo.rebuild(force=True).duration_s >= 0
    assert depbump.rebuild(force=True).duration_s >= 0
    assert depbump.refresh_declared(force=True) >= 0
    assert depbump.refresh_modules() >= 0
    assert lagged.rebuild(force=True).duration_s >= 0


def test_scoring_every_repository_covers_every_repository(mined):
    from git_synapse.analysis import score
    from git_synapse.db.engine import query_one

    enabled = query_one("SELECT count(*) AS n FROM repo WHERE is_enabled")["n"]
    assert len(score.score_all()) == enabled


def test_a_repository_upsert_without_a_connection_is_committed(mined):
    """`git-synapse discover` writes repositories outside any transaction of its own."""
    from git_synapse.db.engine import query_one
    from git_synapse.ingest.store import upsert_repo

    record = RepoRecord(github_id=910099, owner="t", name="e2e-standalone",
                        full_name="t/e2e-standalone", clone_url="",
                        default_branch="main")
    repo_id = upsert_repo(record)
    assert query_one("SELECT id FROM repo WHERE github_id = 910099")["id"] == repo_id
    # Idempotent: discovery runs on every scheduled tick.
    assert upsert_repo(record) == repo_id


def test_reserving_no_ids_does_not_touch_the_sequence(mined):
    """A commit that changed no files reserves zero file ids."""
    from git_synapse.db.engine import connection
    from git_synapse.ingest.store import reserve_ids

    with connection() as conn:
        assert reserve_ids(conn, "file_id_seq", 0) == []
        assert reserve_ids(conn, "file_id_seq", -1) == []
        got = reserve_ids(conn, "file_id_seq", 3)
    assert len(got) == 3 and len(set(got)) == 3


def test_an_author_connection_that_is_already_closed_closes_cleanly(mined):
    """Closing must never mask the real error that ended the run."""
    from git_synapse.db.engine import connection
    from git_synapse.ingest.store import AuthorCache

    with connection() as conn:
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
    from git_synapse.db.engine import connection, query_one
    from git_synapse.ingest.store import FileResolver

    row = query_one("SELECT repo_id, id, path FROM file WHERE repo_id = %s"
                    " ORDER BY id LIMIT 1", (mined,))
    with connection() as conn:
        conn.execute(
            "INSERT INTO file_alias (repo_id, old_path, file_id) VALUES (%s,%s,%s)"
            " ON CONFLICT (repo_id, old_path) DO UPDATE SET file_id = EXCLUDED.file_id",
            (mined, "old/name.py", row["id"]),
        )
        resolver = FileResolver(conn, mined)
        assert resolver.resolve(row["path"]) == row["id"]
        assert resolver.resolve("old/name.py") == row["id"]


def test_a_mirror_that_cannot_be_read_does_not_mark_every_file_deleted(mined,
                                                                       monkeypatch):
    """None means "could not read", not "the tree is empty". Confusing the two
    would tombstone every file in the repository on one bad git invocation."""
    import subprocess as sp

    from git_synapse.analysis.aggregate import _head_tree_paths
    from git_synapse.db.engine import connection

    with connection() as conn:
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
    from git_synapse.db.engine import connection
    from git_synapse.ingest import gitops

    monkeypatch.setattr(gitops, "mirror_path_for",
                        lambda *a, **k: Path("/nonexistent/mirror.git"))
    with connection() as conn:
        assert _head_tree_paths(conn, mined) is None


def test_a_crossrepo_rebuild_with_no_new_commits_keeps_what_it_has(mined, monkeypatch):
    """The partition is expensive; redoing it over an unchanged corpus is pure
    cost, and returning empty stats would look like the data had vanished."""
    from git_synapse.analysis import crossrepo

    crossrepo.rebuild(force=True)
    monkeypatch.setattr(crossrepo, "_build_change_sets", lambda *a, **k: False)
    again = crossrepo.rebuild(force=False)
    # It reports what is already stored rather than zeroes -- empty stats here
    # would read as "the cross-repo data vanished".
    assert again.change_sets > 0
    assert (again.repo_pairs, again.file_pairs) == (0, 0)
    assert again.duration_s >= 0


def test_the_lagged_rebuild_on_an_empty_corpus_writes_nothing(mined, monkeypatch):
    """Injected rather than truncated: emptying `repo` would take the corpus
    every other test in this module is built on with it."""
    import numpy as np

    from git_synapse.analysis import lagged

    monkeypatch.setattr(
        lagged, "_event_matrix",
        lambda *a, **k: (np.zeros((0, 0), dtype=np.float32), [], 0),
    )
    stats = lagged.rebuild(force=True)
    assert stats.n_repos == 0
    assert stats.rows_written == 0


def test_asymmetry_on_a_pair_with_no_lagged_row_carries_no_direction(mined):
    """The ratio is the directional evidence. With nothing on either side there
    is no ratio to report -- and 1.0 would read as "perfectly symmetric"."""
    from git_synapse.analysis import lagged

    row = lagged.asymmetry(-1, -2, lag=1)
    assert row["forward"] is None and row["reverse"] is None
    assert row["ratio"] is None
    assert row["measure"] == "npmi" and row["lag_bins"] == 1


def test_the_symmetric_comparison_says_nothing_without_ground_truth(mined):
    """No manifest bumps means no directed edges to score against, and an
    invented number here would be the one that justifies the whole construction."""
    from git_synapse.analysis import validate

    assert validate.compare_to_symmetric(min_bumps=99999) == {}


def test_an_author_connection_that_refuses_to_close_is_logged_not_raised(mined,
                                                                         monkeypatch):
    """The close happens on the way out of a failing run. Raising here would
    replace the error that actually ended it with one about cleanup."""
    from git_synapse.db.engine import connection
    from git_synapse.ingest.store import AuthorCache

    with connection() as conn:
        cache = AuthorCache(conn)
        monkeypatch.setattr(cache._own, "close",
                            lambda: (_ for _ in ()).throw(OSError("socket gone")))
        cache.close()


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
    from git_synapse.db.engine import query_one
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
        ids[name] = query_one("SELECT id FROM repo WHERE github_id = %s", (gh,))["id"]
    return ids


def test_a_declared_dependency_is_recorded_from_the_manifest(manifests):
    from git_synapse.analysis import depbump
    from git_synapse.db.engine import query

    assert depbump.refresh_declared(force=True) > 0
    rows = query(
        "SELECT dep_repo_id, dep_name, manifest FROM repo_dependency"
        " WHERE consumer_repo_id = %s", (manifests["e2e-mono"],),
    )
    # Stored as the manifest wrote it, so the owner is available at resolution.
    edge = next(r for r in rows if r["dep_name"] == "github.com/acme/e2e-dep")
    assert edge["dep_repo_id"] == manifests["e2e-dep"], "the reference did not resolve"
    assert edge["manifest"] == "go.mod"


def test_the_internal_module_graph_is_recorded_per_manifest(manifests):
    from git_synapse.analysis import depbump
    from git_synapse.db.engine import query

    assert depbump.refresh_modules() > 0
    rows = query(
        "SELECT consumer_module, dep_module, manifest FROM module_dependency"
        " WHERE repo_id = %s", (manifests["e2e-mono"],),
    )
    pairs = {(r["consumer_module"], r["dep_module"]) for r in rows}
    assert ("gateway", "core") in pairs


def test_a_vendored_manifest_is_not_read_as_this_repositorys_dependency(manifests):
    from git_synapse.analysis import depbump
    from git_synapse.db.engine import query

    depbump.refresh_declared(force=True)
    manifest_paths = {
        r["manifest"] for r in query(
            "SELECT manifest FROM repo_dependency WHERE consumer_repo_id = %s",
            (manifests["e2e-mono"],),
        )
    }
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


def test_every_rebuild_also_accepts_the_callers_connection(manifests):
    """The pipeline runs all of these inside one transaction so the derived
    tables land with the run record; nothing had exercised that half."""
    from git_synapse.analysis import aggregate, crossrepo, depbump, lagged, score
    from git_synapse.db.engine import connection

    with connection() as conn:
        assert isinstance(aggregate.repos_needing_aggregation(conn), list)
        assert isinstance(score.score_all(conn), list)
        assert crossrepo.rebuild(conn=conn, force=True).duration_s >= 0
        assert depbump.rebuild(conn=conn, force=True).duration_s >= 0
        assert depbump.refresh_declared(conn=conn, force=True) >= 0
        assert depbump.refresh_modules(conn=conn) >= 0
        assert lagged.rebuild(conn=conn, force=True).duration_s >= 0


def test_a_corpus_with_no_commits_scores_no_pairs(manifests, monkeypatch):
    """`n_total <= 0` is not a corpus where everything scores zero -- every
    measure divides by it."""
    from git_synapse.analysis import crossrepo
    from git_synapse.db.engine import connection

    monkeypatch.setattr(crossrepo, "population", lambda conn: 0)
    with connection() as conn:
        assert crossrepo._score(conn, "file") == 0


def test_no_unpartitioned_commits_means_no_new_change_sets(manifests, monkeypatch):
    """The partition is the expensive half of the cross-repo pass; a tick that
    added no commits must not pay for it."""
    from git_synapse.analysis import crossrepo
    from git_synapse.db.engine import connection

    with connection() as conn:
        crossrepo._build_change_sets(conn, crossrepo.CrossRepoStats(), force=True)
        # Everything is partitioned now, so a second pass finds nothing to do.
        assert crossrepo._build_change_sets(
            conn, crossrepo.CrossRepoStats(), force=False) is False


def test_an_event_matrix_over_an_empty_corpus_has_no_repositories(manifests,
                                                                  monkeypatch):
    """Not an error and not a matrix of zeroes: there is nothing to correlate."""
    from git_synapse.analysis.lagged import _event_matrix
    from git_synapse.db.engine import connection

    class _Empty:
        def fetchall(self):
            return []

    with connection() as conn:
        monkeypatch.setattr(conn, "execute", lambda *a, **k: _Empty())
        matrix, repo_ids, n_bins = _event_matrix(conn, 24)
    assert matrix.shape == (0, 0)
    assert repo_ids == [] and n_bins == 0


def test_a_lag_where_only_self_pairs_survive_writes_nothing(manifests, monkeypatch):
    """The joint matrix is symmetric and its diagonal is meaningless, so a lag
    whose only co-occurrences are a repository with itself contributes nothing."""
    import numpy as np

    from git_synapse.analysis import lagged
    from git_synapse.db.engine import connection

    # One repository, active in one bin: at every lag the only non-zero cell of
    # the joint matrix is the diagonal.
    monkeypatch.setattr(
        lagged, "_event_matrix",
        lambda *a, **k: (np.ones((1, 3), dtype=np.float32), [1], 3),
    )
    with connection() as conn:
        stats = lagged.rebuild(conn=conn, force=True)
    assert stats.n_repos == 1
    assert stats.rows_written == 0


def test_asymmetry_returns_nothing_when_the_query_finds_no_row(manifests,
                                                               monkeypatch):
    from git_synapse.analysis import lagged
    from git_synapse.db import engine

    monkeypatch.setattr(engine, "query_one", lambda *a, **k: None)
    assert lagged.asymmetry(1, 2, lag=1) is None
