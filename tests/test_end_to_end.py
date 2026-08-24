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
