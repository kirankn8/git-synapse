"""Integration tests for the ingest loader."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from git_synapse.db.orm import models, session_scope
from git_synapse.ingest import store
from git_synapse.ingest.github import RepoRecord
from git_synapse.ingest.parser import FileChange, ParsedCommit
from git_synapse.ingest.store import load_commits, upsert_repo

BASE = datetime(2024, 1, 1, tzinfo=UTC)


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
    with session_scope() as conn:
        repo_id = upsert_repo(record, conn)
    yield repo_id
    with session_scope() as session:
        Repo, Commit, File, _Author = models().Repo, models().Commit, models().File, models().Author
        session.query(Commit).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(File).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(Repo).filter_by(id=repo_id).delete(synchronize_session=False)


def counts(repo_id: int) -> tuple[int, int, int]:
    with session_scope() as session:
        Commit, Change, File = models().Commit, models().CommitFile, models().File
        commits = session.query(Commit).filter_by(repo_id=repo_id).count()
        changes = session.query(Change).filter_by(repo_id=repo_id).count()
        files = session.query(File).filter_by(repo_id=repo_id).count()
    return commits, changes, files


def test_loads_commits_and_files(temp_repo):
    commits = [make_commit(i, ["a.py", "b.py"]) for i in range(5)]
    with session_scope() as conn:
        stats = load_commits(temp_repo, commits, conn)
    assert stats.commits_written == 5
    assert stats.changes_written == 10
    assert counts(temp_repo) == (5, 10, 2)


def test_survives_the_flush_boundary(temp_repo, monkeypatch):
    """Rows written after the first flush must not be lost."""
    # What has to happen is crossing the boundary more than once; where the
    # boundary sits does not change the answer. Taking it from the shipped 5000
    # takes this from 10,017 commits to 37, and from the slowest test in the
    # suite by a factor of three to one that does not notice.
    flush = 10
    monkeypatch.setattr(store, "COMMIT_FLUSH_SIZE", flush)
    total = flush * 2 + 17
    modules = 12                      # fewer than `total`, so every one is touched
    commits = (make_commit(i, [f"pkg/mod_{i % modules}.py", "shared.py"])
               for i in range(total))
    with session_scope() as conn:
        stats = load_commits(temp_repo, commits, conn)

    assert stats.commits_read == total
    assert stats.commits_written == total, "commits lost across a flush boundary"
    written_commits, written_changes, written_files = counts(temp_repo)
    assert written_commits == total
    assert written_changes == total * 2, "file changes lost across a flush boundary"
    assert written_files == modules + 1  # the modules, plus shared.py


def test_reingest_is_idempotent(temp_repo):
    """Re-loading the same commits must not duplicate anything."""
    commits = [make_commit(i, ["a.py", "b.py"]) for i in range(20)]
    with session_scope() as conn:
        load_commits(temp_repo, list(commits), conn)
    before = counts(temp_repo)
    with session_scope() as conn:
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
    with session_scope() as conn:
        load_commits(temp_repo, history, conn)

    with session_scope() as session:
        File, Alias = models().File, models().FileAlias
        files = [(r.id, r.path) for r in session.query(File).filter_by(repo_id=temp_repo).order_by(File.path)]
        aliases = [(r.old_path, r.file_id) for r in session.query(Alias).filter_by(repo_id=temp_repo)]

    assert len(files) == 1, f"rename should not create a second file row: {files}"
    file_id, path = files[0]
    assert path == "new/name.go", "file.path must hold the most recent name"
    assert aliases == [("old/name.go", file_id)]

    with session_scope() as session:
        n = session.query(models().CommitFile).filter_by(file_id=file_id).count()
    assert n == 4, "all four commits must attach to the single surviving file id"


def test_oversized_commits_are_stored_but_not_pair_eligible(temp_repo):
    """The fan-out cap must exclude a commit from pairing without discarding it."""
    small = make_commit(0, ["a.py", "b.py"])
    huge = make_commit(1, [f"f{i}.py" for i in range(200)])
    with session_scope() as conn:
        stats = load_commits(temp_repo, [small, huge], conn, max_files_per_commit=60)

    assert stats.skipped_oversized == 1
    assert stats.pair_eligible == 1
    with session_scope() as session:
        Commit = models().Commit
        rows = [(r.n_files, r.pair_eligible) for r in session.query(Commit).filter_by(repo_id=temp_repo).order_by(Commit.n_files)]
    assert rows == [(2, True), (200, False)]
    # The oversized commit's changes are still recorded in full.
    assert counts(temp_repo)[1] == 202


def test_merge_commits_are_not_pair_eligible(temp_repo):
    merge = make_commit(1, ["a.py", "b.py"], parents=[f"{0:040x}", f"{99:040x}"])
    with session_scope() as conn:
        stats = load_commits(temp_repo, [merge], conn)
    assert stats.pair_eligible == 0
    with session_scope() as session:
        row = session.query(models().Commit.is_merge, models().Commit.pair_eligible).filter_by(repo_id=temp_repo).one()
    assert row == (True, False)


def test_duplicate_paths_within_one_commit_are_collapsed(temp_repo):
    """The (commit_id, file_id) primary key must not be violated."""
    commit = make_commit(0, ["a.py", "a.py", "b.py"])
    with session_scope() as conn:
        stats = load_commits(temp_repo, [commit], conn)
    assert stats.changes_written == 2
    assert counts(temp_repo)[1] == 2


def test_rename_onto_an_occupied_path_does_not_cross_identities(temp_repo):
    """A rename whose destination another file already holds must not be carried."""
    repo_id = temp_repo

    both = make_commit(1, ["old.txt", "new.txt"])
    rename = make_commit(2, [])
    rename.files = [FileChange(path="new.txt", change_type="R", old_path="old.txt",
                               similarity=100, insertions=1, deletions=0)]
    later = make_commit(3, ["new.txt"])
    with session_scope() as conn:
        load_commits(repo_id, [both, rename, later], conn)

    with session_scope() as session:
        File = models().File
        rows = {r.path: r.id for r in session.query(File).filter_by(repo_id=repo_id)}
    assert "new.txt" in rows, "the occupant must keep its own row"
    assert rows.get("old.txt") != rows.get("new.txt"), "identities must stay separate"

    with session_scope() as session:
        Commit, Change = models().Commit, models().CommitFile
        owner = session.query(Change.file_id).join(Commit, Commit.id == Change.commit_id).filter(
            Commit.sha == f"{3:040x}", Change.repo_id == repo_id).scalar()
    assert owner == rows["new.txt"], (
        "a later change must land on the file that lives at that path"
    )


def test_empty_repository_reads_as_no_commits_not_an_error(tmp_path):
    """A repository with no commits has no HEAD to resolve."""
    import subprocess

    from git_synapse.ingest.parser import iter_commits

    mirror = tmp_path / "empty.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(mirror)], check=True)

    assert list(iter_commits(mirror)) == []


def test_a_refresh_does_not_blank_the_metadata_discovery_collected(db):
    """`load_repo_records` rebuilds a record from the database and hands it straight back to `upsert_repo`."""
    from git_synapse.db.orm import session_scope
    from git_synapse.ingest.pipeline import load_repo_records

    full = RepoRecord(
        github_id=777001, owner="acme", name="meta-probe", full_name="acme/meta-probe",
        clone_url="https://example.invalid/x.git", default_branch="main",
        description="a description", primary_language="Rust",
        topics=["a", "b"], license_spdx="MIT", stargazers=42,
    )
    with session_scope() as conn:
        upsert_repo(full, conn)

    reloaded = [r for r in load_repo_records() if r.full_name == "acme/meta-probe"]
    assert reloaded, "the repository must come back from the database"
    with session_scope() as conn:
        upsert_repo(reloaded[0], conn)

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(full_name="acme/meta-probe").one()
        assert row.primary_language == "Rust"
        assert row.description == "a description"
        assert row.topics == ["a", "b"]
        assert row.license_spdx == "MIT"
        assert row.stargazers == 42
        session.delete(row)


def test_tags_are_indexed_and_resolved_to_their_commit(db):
    """The tag loader was unreachable while mirrors excluded tags, so nothing exercised it: the first real tag must be persisted and resolved by the ORM."""
    from git_synapse.db.orm import session_scope
    from git_synapse.ingest.gitops import Tag
    from git_synapse.ingest.store import load_tags

    record = RepoRecord(github_id=777002, owner="acme", name="tagged",
                        full_name="acme/tagged", clone_url="", default_branch="main")
    with session_scope() as conn:
        repo_id = upsert_repo(record, conn)
        load_commits(repo_id, [make_commit(0, ["a.py"])], conn)
        sha = conn.query(models().Commit).filter_by(repo_id=repo_id).one().sha
        written = load_tags(repo_id, [
            Tag(name="v1.0.0", commit_sha=sha, tagged_at=BASE, annotated=False),
            Tag(name="v1.1.0", commit_sha="f" * 40, tagged_at=BASE, annotated=True),
        ], conn)

    assert written == 2
    with session_scope() as session:
        rows = {r.name: r for r in session.query(models().RefTag).filter_by(repo_id=repo_id)}
        assert rows["v1.0.0"].commit_id is not None, "a tag on an ingested commit resolves"
        assert rows["v1.1.0"].commit_id is None, "a tag off the shipped branch stays unresolved"
        assert rows["v1.1.0"].annotated is True

    # Replaced wholesale, so a deleted or moved tag cannot linger.
    with session_scope() as conn:
        load_tags(repo_id, [Tag(name="v2.0.0", commit_sha=sha, tagged_at=BASE, annotated=False)], conn)
    with session_scope() as session:
        assert {r.name for r in session.query(models().RefTag).filter_by(repo_id=repo_id)} == {"v2.0.0"}
        session.query(models().CommitFile).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().Commit).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().File).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().Repo).filter_by(id=repo_id).delete(synchronize_session=False)


def test_a_record_from_a_host_with_no_api_cannot_blank_what_one_collected(db):
    """GitProvider knows nothing about stars, forks or visibility and says so by leaving the defaults."""
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.store import upsert_repo

    rich = RepoRecord(
        github_id=99001, owner="acme", name="sparse-test",
        full_name="acme/sparse-test", provider="github", host="github.com",
        clone_url="https://github.com/acme/sparse-test.git",
        primary_language="Rust", visibility="public", stargazers=4200,
        is_fork=True, is_archived=True, disk_usage_kb=512, forks_count=7,
    )
    repo_id = upsert_repo(rich)

    # The same repository seen again through the no-API fallback.
    bare = RepoRecord(
        github_id=None, owner="acme", name="sparse-test",
        full_name="acme/sparse-test", provider="git", host="github.com",
        clone_url="https://github.com/acme/sparse-test.git",
        visibility="unknown",
    )
    assert upsert_repo(bare) == repo_id, "same repository, not a second row"

    with session_scope() as session:
        row = session.get(models().Repo, repo_id)
        assert row.stargazers == 4200
        assert row.is_fork is True and row.is_archived is True
        assert row.visibility == "public", "not overwritten with 'unknown'"
        assert row.forks_count == 7 and row.disk_usage_kb == 512
        assert row.primary_language == "Rust"
        session.delete(row)


def test_a_real_api_record_still_updates_those_fields(db):
    """The guard must not freeze them: a repository really does get archived."""
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.store import upsert_repo

    def record(**over):
        base = {"github_id": 99002, "owner": "acme", "name": "live-test",
                "full_name": "acme/live-test", "provider": "github",
                "host": "github.com", "visibility": "public", "stargazers": 1}
        base.update(over)
        return RepoRecord(**base)

    repo_id = upsert_repo(record())
    upsert_repo(record(stargazers=9, is_archived=True, visibility="private"))
    with session_scope() as session:
        row = session.get(models().Repo, repo_id)
        assert (row.stargazers, row.is_archived, row.visibility) == (9, True, "private")
        session.delete(row)



def test_an_author_seen_again_gains_a_display_name_and_keeps_the_old_one(db):
    """One person commits as "j.doe" and later as "Jane Doe" from the same address."""
    from uuid import uuid4

    from git_synapse.ingest.store import AuthorCache

    email = f"jane-{uuid4().hex[:8]}@example.com"
    with session_scope() as session:
        cache = AuthorCache(session)
        first = cache.resolve(email, "")
        assert first is not None

        # A second cache, so the in-process shortcut does not hide the lookup.
        again = AuthorCache(session).resolve(email, "Jane Doe")
        assert again == first

        row = session.get(models().Author, first)
        assert row.display_name == "Jane Doe"
        assert row.known_names == ["Jane Doe"]

        AuthorCache(session).resolve(email, "j.doe")
        row = session.get(models().Author, first)
        assert row.display_name == "Jane Doe"
        assert row.known_names == ["Jane Doe", "j.doe"]

        # Already known, so it is not appended twice.
        AuthorCache(session).resolve(email, "j.doe")
        row = session.get(models().Author, first)
        assert row.known_names == ["Jane Doe", "j.doe"]


def test_a_rename_onto_a_file_that_vanished_is_skipped(db):
    """The rename is applied at flush time, by which point the row it names can have been deleted -- an unreachable-commit sweep runs in the same pass."""
    from git_synapse.ingest.store import FileResolver

    with session_scope() as session:
        resolver = FileResolver(session, repo_id=1)
        resolver._pending_renames = {999_999_999: "moved/elsewhere.py"}
        resolver._pending_aliases = []
        assert resolver.flush() == 0
