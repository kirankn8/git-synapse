"""ORM persistence for parsed Git history."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from git_synapse.analysis.manifests import version_key
from git_synapse.config import get_config
from git_synapse.db.orm import models, session_scope
from git_synapse.ingest.github import RepoRecord
from git_synapse.ingest.parser import ParsedCommit, split_path

log = logging.getLogger(__name__)
COMMIT_FLUSH_SIZE = 5000


def load_tags(repo_id: int, tags: list, conn: object) -> int:
    RefTag, Commit = models().RefTag, models().Commit
    conn.query(RefTag).filter_by(repo_id=repo_id).delete(synchronize_session=False)
    commits = {row.sha: row.id for row in conn.query(Commit).filter_by(repo_id=repo_id).all()}
    conn.add_all([
        RefTag(repo_id=repo_id, name=tag.name, commit_sha=tag.commit_sha, tagged_at=tag.tagged_at,
               annotated=tag.annotated, commit_id=commits.get(tag.commit_sha), main_sha=tag.main_sha,
               main_commit_id=commits.get(tag.main_sha), version_key=version_key(tag.name))
        for tag in tags
    ])
    return len(tags)


def upsert_repo(record: RepoRecord, conn: object | None = None, account_id: int | None = None) -> int:
    """Insert or update a repository while preserving richer API data."""
    Repo = models().Repo

    def write(session: object) -> int:
        row = session.query(Repo).filter_by(host=record.host, full_name=record.full_name).first()
        values = {
            "github_id": record.github_id, "provider": record.provider, "host": record.host,
            "owner": record.owner, "name": record.name, "full_name": record.full_name,
            "description": record.description, "homepage": record.homepage, "html_url": record.html_url,
            "clone_url": record.clone_url, "ssh_url": record.ssh_url, "default_branch": record.default_branch,
            "primary_language": record.primary_language, "languages": record.languages,
            "topics": list(record.topics), "license_spdx": record.license_spdx, "visibility": record.visibility,
            "is_private": record.is_private, "is_fork": record.is_fork, "is_archived": record.is_archived,
            "is_template": record.is_template, "is_disabled": record.is_disabled, "disk_usage_kb": record.disk_usage_kb,
            "stargazers": record.stargazers, "watchers": record.watchers, "forks_count": record.forks_count,
            "open_issues": record.open_issues, "github_created_at": record.github_created_at,
            "github_updated_at": record.github_updated_at, "github_pushed_at": record.github_pushed_at,
            "raw_github": record.raw, "account_id": account_id,
        }
        if row is None:
            row = Repo(**values)
            session.add(row)
        else:
            for key, value in values.items():
                if key == "account_id" and value is None:
                    continue
                if record.provider == "git" and key in {
                    "visibility", "is_private", "is_fork", "is_archived", "is_template", "is_disabled",
                    "disk_usage_kb", "stargazers", "watchers", "forks_count", "open_issues",
                    "github_created_at", "github_updated_at", "github_pushed_at",
                }:
                    continue
                if key in {"description", "primary_language", "license_spdx"} and value is None:
                    continue
                if key == "raw_github" and value == {}:
                    continue
                setattr(row, key, value)
        session.flush()
        return int(row.id)

    if conn is not None:
        return write(conn)
    with session_scope() as session:
        return write(session)


class AuthorCache:
    """Resolve canonical author emails through one ORM session."""

    def __init__(self, conn: object) -> None:
        self._session = conn
        self._cache: dict[str, int] = {}

    def close(self) -> None:
        return None

    def resolve(self, email: str, name: str) -> int | None:
        email = (email or "").strip().lower()
        if not email:
            return None
        if email in self._cache:
            return self._cache[email]
        Author = models().Author
        row = self._session.query(Author).filter_by(email=email).first()
        if row is None:
            row = Author(email=email, display_name=name or None, known_names=[name] if name else [])
            self._session.add(row)
            self._session.flush()
        else:
            if not row.display_name and name:
                row.display_name = name
            if name and name not in (row.known_names or []):
                row.known_names = [*(row.known_names or []), name]
        self._cache[email] = int(row.id)
        return int(row.id)


class FileResolver:
    """Resolve current paths and aliases to stable file ORM rows."""

    def __init__(self, conn: object, repo_id: int) -> None:
        self._session, self._repo_id = conn, repo_id
        self._by_path: dict[str, int] = {}
        self._pending_renames: dict[int, str] = {}
        self._pending_aliases: list[tuple[str, int]] = []
        self._load()

    def _load(self) -> None:
        File, Alias = models().File, models().FileAlias
        for row in self._session.query(File).filter_by(repo_id=self._repo_id).all():
            self._by_path[row.path] = int(row.id)
        for row in self._session.query(Alias).filter_by(repo_id=self._repo_id).all():
            self._by_path.setdefault(row.old_path, int(row.file_id))

    def resolve(self, path: str, old_path: str | None = None) -> int:
        if old_path and old_path in self._by_path:
            existing = self._by_path[old_path]
            occupant = self._by_path.get(path)
            if occupant is not None and occupant != existing:
                return occupant
            self._by_path[path] = existing
            self._pending_renames[existing] = path
            if old_path != path:
                self._pending_aliases.append((old_path, existing))
            return existing
        if path in self._by_path:
            return self._by_path[path]
        File = models().File
        directory, basename, extension, depth = split_path(path)
        row = File(repo_id=self._repo_id, path=path, dir_path=directory, basename=basename,
                   extension=extension, depth=depth)
        self._session.add(row)
        self._session.flush()
        self._by_path[path] = int(row.id)
        return int(row.id)

    def flush(self) -> int:
        File, Alias = models().File, models().FileAlias
        created = 0
        for file_id, path in self._pending_renames.items():
            row = self._session.get(File, file_id)
            if row is None:
                continue
            occupant = self._session.query(File).filter(
                File.repo_id == self._repo_id, File.path == path, File.id != file_id,
            ).first()
            if occupant is None:
                row.path = path
                row.dir_path, row.basename, row.extension, row.depth = split_path(path)
        for old_path, file_id in self._pending_aliases:
            exists = self._session.query(Alias).filter_by(
                repo_id=self._repo_id, old_path=old_path,
            ).first()
            if exists is None:
                self._session.add(Alias(repo_id=self._repo_id, old_path=old_path, file_id=file_id))
                created += 1
        self._pending_renames.clear()
        self._pending_aliases.clear()
        return created


@dataclass
class LoadStats:
    commits_read: int = 0
    commits_written: int = 0
    files_created: int = 0
    changes_written: int = 0
    pair_eligible: int = 0
    skipped_oversized: int = 0
    newest_sha: str | None = None


def load_commits(repo_id: int, commits: Iterable[ParsedCommit], conn: object,
                 max_files_per_commit: int | None = None) -> LoadStats:
    """Persist parsed commits in oldest-first order through ORM objects."""
    Commit, Change, Parent = models().Commit, models().CommitFile, models().CommitParent
    cfg = get_config().ingest
    cap = max_files_per_commit if max_files_per_commit is not None else cfg.max_files_per_commit
    authors, files, stats = AuthorCache(conn), FileResolver(conn, repo_id), LoadStats()
    try:
        for parsed in commits:
            stats.commits_read += 1
            stats.newest_sha = parsed.sha
            n_files = len(parsed.files)
            oversized = cap > 0 and n_files > cap
            eligible = (not parsed.is_merge) and n_files > 0 and not oversized
            stats.skipped_oversized += int(oversized)
            stats.pair_eligible += int(eligible)
            commit = conn.query(Commit).filter_by(repo_id=repo_id, sha=parsed.sha).first()
            if commit is None:
                commit = Commit(repo_id=repo_id, sha=parsed.sha,
                                author_id=authors.resolve(parsed.author_email, parsed.author_name),
                                committer_id=authors.resolve(parsed.committer_email, parsed.committer_name),
                                authored_at=parsed.authored_at, committed_at=parsed.committed_at,
                                subject=parsed.subject[:2000], body=parsed.body[:20000] if parsed.body else None,
                                parent_count=len(parsed.parents), is_merge=parsed.is_merge, n_files=n_files,
                                insertions=parsed.insertions, deletions=parsed.deletions, pair_eligible=eligible)
                conn.add(commit)
                conn.flush()
                stats.commits_written += 1
            seen: set[int] = set()
            for change in parsed.files:
                file_id = files.resolve(change.path, change.old_path)
                if file_id in seen:
                    continue
                seen.add(file_id)
                if conn.query(Change).filter_by(commit_id=commit.id, file_id=file_id).first() is None:
                    conn.add(Change(commit_id=commit.id, file_id=file_id, repo_id=repo_id,
                                    change_type=change.change_type, insertions=change.insertions,
                                    deletions=change.deletions, is_binary=change.is_binary,
                                    old_path=change.old_path, similarity=change.similarity))
                    stats.changes_written += 1
            for ordinal, parent_sha in enumerate(parsed.parents):
                if conn.query(Parent).filter_by(
                    repo_id=repo_id, child_sha=parsed.sha, parent_sha=parent_sha,
                ).first() is None:
                    conn.add(Parent(repo_id=repo_id, child_sha=parsed.sha, parent_sha=parent_sha, ordinal=ordinal))
            if stats.commits_read % COMMIT_FLUSH_SIZE == 0:
                conn.flush()
                # Flushing writes the rows but leaves every instance in the identity map,
                # so a large history ends up holding all of them at once. Nothing below
                # reads them back through the session.
                conn.expunge_all()
        stats.files_created += files.flush()
        conn.flush()
    finally:
        authors.close()
    return stats
