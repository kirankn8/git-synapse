"""Integration tests for the ingest loader.

These run against a real Postgres because the behaviour under test -- batched
COPY, sequence-block id allocation, rename identity and idempotent re-ingest --
is defined by the database, not by Python. Mocking it would test nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from git_synapse.db.engine import connection, query, query_one
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


def test_rename_onto_an_occupied_path_does_not_cross_identities(temp_repo):
    """A rename whose destination another file already holds must not be carried.

    flush() guards its UPDATE against violating the unique index, so the row kept
    its old path while the in-memory map pointed the new path at it. Every later
    commit to that path landed on the wrong file, and the file that genuinely
    lived there sat frozen -- 10,586 rows across 55 repositories.
    """
    repo_id = temp_repo

    both = make_commit(1, ["old.txt", "new.txt"])
    rename = make_commit(2, [])
    rename.files = [FileChange(path="new.txt", change_type="R", old_path="old.txt",
                               similarity=100, insertions=1, deletions=0)]
    later = make_commit(3, ["new.txt"])
    with connection() as conn:
        load_commits(repo_id, [both, rename, later], conn)

    rows = {r["path"]: r["id"] for r in query(
        "SELECT id, path FROM file WHERE repo_id = %s", (repo_id,))}
    assert "new.txt" in rows, "the occupant must keep its own row"
    assert rows.get("old.txt") != rows.get("new.txt"), "identities must stay separate"

    owner = query_one(
        """
        SELECT cf.file_id FROM commit_file cf
        JOIN commit c ON c.id = cf.commit_id
        WHERE c.sha = %s AND cf.repo_id = %s
        """,
        (f"{3:040x}", repo_id),
    )
    assert owner["file_id"] == rows["new.txt"], (
        "a later change must land on the file that lives at that path"
    )


def test_empty_repository_reads_as_no_commits_not_an_error(tmp_path):
    """A repository with no commits has no HEAD to resolve.

    `git log --all` returned nothing and exited 0; scoping the walk to the
    default branch made git fail with "Needed a single revision" and turned
    three empty repositories into hard ingest failures.
    """
    import subprocess

    from git_synapse.ingest.parser import iter_commits

    mirror = tmp_path / "empty.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(mirror)], check=True)

    assert list(iter_commits(mirror)) == []


def test_a_refresh_does_not_blank_the_metadata_discovery_collected(db):
    """`load_repo_records` rebuilds a record from the database and hands it
    straight back to `upsert_repo`. When it read only the columns the pipeline
    needed, every ingest wiped language, description, topics and stars."""
    from git_synapse.db.engine import connection, query_one
    from git_synapse.ingest.pipeline import load_repo_records

    full = RepoRecord(
        github_id=777001, owner="acme", name="meta-probe", full_name="acme/meta-probe",
        clone_url="https://example.invalid/x.git", default_branch="main",
        description="a description", primary_language="Rust",
        topics=["a", "b"], license_spdx="MIT", stargazers=42,
    )
    with connection() as conn:
        upsert_repo(full, conn)

    reloaded = [r for r in load_repo_records() if r.full_name == "acme/meta-probe"]
    assert reloaded, "the repository must come back from the database"
    with connection() as conn:
        upsert_repo(reloaded[0], conn)

    row = query_one("SELECT primary_language, description, topics, license_spdx,"
                    " stargazers FROM repo WHERE full_name = 'acme/meta-probe'")
    assert row["primary_language"] == "Rust"
    assert row["description"] == "a description"
    assert row["topics"] == ["a", "b"]
    assert row["license_spdx"] == "MIT"
    assert row["stargazers"] == 42

    from git_synapse.db.engine import execute
    execute("DELETE FROM repo WHERE full_name = 'acme/meta-probe'")


def test_tags_are_indexed_and_resolved_to_their_commit(db):
    """The tag loader was unreachable while mirrors excluded tags, so nothing
    exercised it: the first real tag hit `Connection.executemany`, which psycopg
    puts on the cursor."""
    from git_synapse.db.engine import connection, execute, query
    from git_synapse.ingest.gitops import Tag
    from git_synapse.ingest.store import load_tags

    record = RepoRecord(github_id=777002, owner="acme", name="tagged",
                        full_name="acme/tagged", clone_url="", default_branch="main")
    with connection() as conn:
        repo_id = upsert_repo(record, conn)
        load_commits(repo_id, [make_commit(0, ["a.py"])], conn)
        # Read through the same connection: the commits are not committed yet,
        # and the query helper checks out a different one from the pool.
        sha = conn.execute("SELECT sha FROM commit WHERE repo_id = %s",
                           (repo_id,)).fetchone()[0]
        written = load_tags(repo_id, [
            Tag(name="v1.0.0", commit_sha=sha, tagged_at=BASE, annotated=False),
            Tag(name="v1.1.0", commit_sha="f" * 40, tagged_at=BASE, annotated=True),
        ], conn)

    assert written == 2
    rows = {r["name"]: r for r in query(
        "SELECT name, commit_id, annotated FROM ref_tag WHERE repo_id = %s", (repo_id,))}
    assert rows["v1.0.0"]["commit_id"] is not None, "a tag on an ingested commit resolves"
    assert rows["v1.1.0"]["commit_id"] is None, "a tag off the shipped branch stays unresolved"
    assert rows["v1.1.0"]["annotated"] is True

    # Replaced wholesale, so a deleted or moved tag cannot linger.
    with connection() as conn:
        load_tags(repo_id, [Tag(name="v2.0.0", commit_sha=sha, tagged_at=BASE, annotated=False)], conn)
    assert {r["name"] for r in query(
        "SELECT name FROM ref_tag WHERE repo_id = %s", (repo_id,))} == {"v2.0.0"}
    execute("DELETE FROM repo WHERE id = %s", (repo_id,))
