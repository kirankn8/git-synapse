"""Integration tests for the ingest loader.

These run against a real Postgres because the behaviour under test -- batched
COPY, sequence-block id allocation, rename identity and idempotent re-ingest --
is defined by the database, not by Python. Mocking it would test nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from git_synapse.db.engine import connection
from git_synapse.ingest.github import RepoRecord
from git_synapse.ingest.parser import FileChange, ParsedCommit
from git_synapse.ingest.store import COMMIT_FLUSH_SIZE, load_commits, upsert_repo

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_commit(i: int, paths: list[str], parents: list[str] | None = None, **kw) -> ParsedCommit:
    """Build a synthetic commit touching `paths`."""
    return ParsedCommit(
        sha=f"{i:040x}",
        parents=parents if parents is not None else ([f"{i - 1:040x}"] if i else []),
        author_name="Test Author",
        author_email="test@example.com",
        authored_at=BASE + timedelta(hours=i),
        committer_name="Test Author",
        committer_email="test@example.com",
        committed_at=BASE + timedelta(hours=i),
        subject=f"commit {i}",
        body="",
        files=[FileChange(path=p, change_type="M", insertions=1, deletions=1) for p in paths],
        **kw,
    )


@pytest.fixture
def temp_repo(scratch_db):
    """Create an isolated repository row, and remove it afterwards."""
    record = RepoRecord(
        github_id=999_000_001,
        owner="test",
        name="store-fixture",
        full_name="test/store-fixture",
        clone_url="https://example.invalid/test/store-fixture.git",
    )
    with connection() as conn:
        repo_id = upsert_repo(record, conn)
        conn.execute("DELETE FROM commit WHERE repo_id = %s", (repo_id,))
        conn.execute("DELETE FROM file WHERE repo_id = %s", (repo_id,))
    yield repo_id
    with connection() as conn:
        conn.execute("DELETE FROM repo WHERE id = %s", (repo_id,))


def counts(repo_id: int) -> tuple[int, int, int]:
    with connection() as conn:
        commits = conn.execute("SELECT count(*) FROM commit WHERE repo_id=%s", (repo_id,)).fetchone()[0]
        changes = conn.execute("SELECT count(*) FROM commit_file WHERE repo_id=%s", (repo_id,)).fetchone()[0]
        files = conn.execute("SELECT count(*) FROM file WHERE repo_id=%s", (repo_id,)).fetchone()[0]
    return commits, changes, files


def test_loads_commits_and_files(temp_repo):
    commits = [make_commit(i, ["a.py", "b.py"]) for i in range(5)]
    with connection() as conn:
        stats = load_commits(temp_repo, commits, conn)
    assert stats.commits_written == 5
    assert stats.changes_written == 10
    assert counts(temp_repo) == (5, 10, 2)


def test_survives_the_flush_boundary(temp_repo):
    """Rows written after the first flush must not be lost.

    The loader batches into ``COMMIT_FLUSH_SIZE`` chunks. An earlier refactor
    rebound the row lists inside ``flush()`` while an inner scope still held the
    old objects, which silently dropped every commit after the first batch. This
    test crosses the boundary twice so that regression cannot return.
    """
    total = COMMIT_FLUSH_SIZE * 2 + 17
    commits = (make_commit(i, [f"pkg/mod_{i % 50}.py", "shared.py"]) for i in range(total))
    with connection() as conn:
        stats = load_commits(temp_repo, commits, conn)

    assert stats.commits_read == total
    assert stats.commits_written == total, "commits lost across a flush boundary"
    written_commits, written_changes, written_files = counts(temp_repo)
    assert written_commits == total
    assert written_changes == total * 2, "file changes lost across a flush boundary"
    assert written_files == 51  # 50 modules + shared.py


def test_reingest_is_idempotent(temp_repo):
    """Re-loading the same commits must not duplicate anything."""
    commits = [make_commit(i, ["a.py", "b.py"]) for i in range(20)]
    with connection() as conn:
        load_commits(temp_repo, list(commits), conn)
    before = counts(temp_repo)
    with connection() as conn:
        load_commits(temp_repo, list(commits), conn)
    assert counts(temp_repo) == before


def test_rename_preserves_file_identity(temp_repo):
    """A renamed file keeps one id, one history, and gains an alias row."""
    history = [
        make_commit(0, ["old/name.go"]),
        make_commit(1, ["old/name.go"]),
        ParsedCommit(
            sha=f"{2:040x}",
            parents=[f"{1:040x}"],
            author_name="Test Author",
            author_email="test@example.com",
            authored_at=BASE + timedelta(hours=2),
            committer_name="Test Author",
            committer_email="test@example.com",
            committed_at=BASE + timedelta(hours=2),
            subject="move it",
            body="",
            files=[FileChange(path="new/name.go", change_type="R", old_path="old/name.go", similarity=98)],
        ),
        make_commit(3, ["new/name.go"]),
    ]
    with connection() as conn:
        load_commits(temp_repo, history, conn)

    with connection() as conn:
        files = conn.execute(
            "SELECT id, path FROM file WHERE repo_id=%s ORDER BY path", (temp_repo,)
        ).fetchall()
        aliases = conn.execute(
            "SELECT old_path, file_id FROM file_alias WHERE repo_id=%s", (temp_repo,)
        ).fetchall()

    assert len(files) == 1, f"rename should not create a second file row: {files}"
    file_id, path = files[0]
    assert path == "new/name.go", "file.path must hold the most recent name"
    assert aliases == [("old/name.go", file_id)]

    with connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM commit_file WHERE file_id=%s", (file_id,)
        ).fetchone()[0]
    assert n == 4, "all four commits must attach to the single surviving file id"


def test_oversized_commits_are_stored_but_not_pair_eligible(temp_repo):
    """The fan-out cap must exclude a commit from pairing without discarding it."""
    small = make_commit(0, ["a.py", "b.py"])
    huge = make_commit(1, [f"f{i}.py" for i in range(200)])
    with connection() as conn:
        stats = load_commits(temp_repo, [small, huge], conn, max_files_per_commit=60)

    assert stats.skipped_oversized == 1
    assert stats.pair_eligible == 1
    with connection() as conn:
        rows = conn.execute(
            "SELECT n_files, pair_eligible FROM commit WHERE repo_id=%s ORDER BY n_files",
            (temp_repo,),
        ).fetchall()
    assert rows == [(2, True), (200, False)]
    # The oversized commit's changes are still recorded in full.
    assert counts(temp_repo)[1] == 202


def test_merge_commits_are_not_pair_eligible(temp_repo):
    merge = make_commit(1, ["a.py", "b.py"], parents=[f"{0:040x}", f"{99:040x}"])
    with connection() as conn:
        stats = load_commits(temp_repo, [merge], conn)
    assert stats.pair_eligible == 0
    with connection() as conn:
        row = conn.execute(
            "SELECT is_merge, pair_eligible FROM commit WHERE repo_id=%s", (temp_repo,)
        ).fetchone()
    assert row == (True, False)


def test_duplicate_paths_within_one_commit_are_collapsed(temp_repo):
    """The (commit_id, file_id) primary key must not be violated."""
    commit = make_commit(0, ["a.py", "a.py", "b.py"])
    with connection() as conn:
        stats = load_commits(temp_repo, [commit], conn)
    assert stats.changes_written == 2
    assert counts(temp_repo)[1] == 2
