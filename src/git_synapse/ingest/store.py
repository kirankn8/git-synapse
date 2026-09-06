"""Persistence of parsed git history into Postgres.

Identity across renames
-----------------------
A file's statistical history is only meaningful if the file keeps one identity
when it moves. Git reports a move as ``R090 old new``, so the loader walks
history **oldest first** and, on each rename, keeps the existing ``file`` row and
merely repoints its ``path`` at the new name while recording the old name in
``file_alias``. A file moved three times therefore has one row, one id, one
continuous change history, and three alias rows -- rather than four unrelated
rows each holding a fragment of the truth.

Walking oldest-first is what makes this a single pass: at the moment a rename is
seen, the source path has always already been observed.

Id allocation
-------------
New ``file`` and ``commit`` ids are drawn from the Postgres sequences in blocks
(``SELECT nextval(...) FROM generate_series``) rather than one round trip at a
time. That lets Python assign real primary keys before writing, so rows go
straight into COPY with their foreign keys already resolved, and it stays
correct when several repositories are ingested concurrently.

Writes go through a COPY-into-temp then INSERT ... ON CONFLICT DO NOTHING merge,
which is both the fastest bulk path psycopg offers and idempotent -- a re-run
after a force-push cannot create duplicates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from git_synapse.analysis.manifests import version_key
from git_synapse.config import get_config
from git_synapse.db.engine import connection, copy_rows
from git_synapse.ingest.github import RepoRecord
from git_synapse.ingest.parser import ParsedCommit, split_path

log = logging.getLogger(__name__)

#: Commits buffered before a flush. Bounds memory on repos with long histories.
COMMIT_FLUSH_SIZE = 5000


def load_tags(repo_id: int, tags: list, conn: psycopg.Connection) -> int:
    """Replace a repository's tag index, resolving each to an ingested commit.

    Replaced wholesale rather than merged: a tag can be deleted or force-moved
    upstream, and a stale row would resolve a version to a commit that release
    no longer names.
    """
    conn.execute("DELETE FROM ref_tag WHERE repo_id = %s", (repo_id,))
    if not tags:
        return 0
    # executemany lives on the cursor in psycopg 3, not on the connection.
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ref_tag (repo_id, name, commit_sha, tagged_at, annotated,
                                 commit_id, main_sha, main_commit_id, version_key)
            VALUES (%s, %s, %s, %s, %s,
                    (SELECT id FROM commit WHERE repo_id = %s AND sha = %s),
                    %s,
                    (SELECT id FROM commit WHERE repo_id = %s AND sha = %s),
                    %s)
            ON CONFLICT (repo_id, name) DO NOTHING
            """,
            [(repo_id, t.name, t.commit_sha, t.tagged_at, t.annotated,
              repo_id, t.commit_sha, t.main_sha, repo_id, t.main_sha,
              version_key(t.name))
             for t in tags],
        )
    return len(tags)


def upsert_repo(record: RepoRecord, conn: psycopg.Connection | None = None, account_id: int | None = None) -> int:
    """Insert or update a repository row and return its id.

    Every field the host reported is written, including the untouched payload
    in ``raw_github`` so that unmodelled fields remain available later.

    The conflict target is ``(host, full_name)``, not ``full_name``: the same
    ``owner/name`` genuinely exists on more than one host, and merging two
    histories into one row would be undetectable from the outside.
    """

    def _run(c: psycopg.Connection) -> int:
        row = c.execute(
            """
            INSERT INTO repo (
                github_id, provider, host, owner, name, full_name, description, homepage,
                html_url, clone_url, ssh_url, default_branch, primary_language,
                languages, topics, license_spdx, visibility, is_private, is_fork,
                is_archived, is_template, is_disabled, disk_usage_kb, stargazers,
                watchers, forks_count, open_issues, github_created_at,
                github_updated_at, github_pushed_at, raw_github, account_id, updated_at
            ) VALUES (
                %(github_id)s, %(provider)s, %(host)s, %(owner)s, %(name)s,
                %(full_name)s, %(description)s,
                %(homepage)s, %(html_url)s, %(clone_url)s, %(ssh_url)s,
                %(default_branch)s, %(primary_language)s, %(languages)s, %(topics)s,
                %(license_spdx)s, %(visibility)s, %(is_private)s, %(is_fork)s,
                %(is_archived)s, %(is_template)s, %(is_disabled)s, %(disk_usage_kb)s,
                %(stargazers)s, %(watchers)s, %(forks_count)s, %(open_issues)s,
                %(github_created_at)s, %(github_updated_at)s, %(github_pushed_at)s,
                %(raw_github)s, %(account_id)s, now()
            )
            ON CONFLICT (host, full_name) DO UPDATE SET
                github_id         = EXCLUDED.github_id,
                provider          = EXCLUDED.provider,
                description       = COALESCE(EXCLUDED.description, repo.description),
                homepage          = COALESCE(EXCLUDED.homepage, repo.homepage),
                html_url          = EXCLUDED.html_url,
                clone_url         = EXCLUDED.clone_url,
                ssh_url           = EXCLUDED.ssh_url,
                default_branch    = EXCLUDED.default_branch,
                primary_language  = COALESCE(EXCLUDED.primary_language, repo.primary_language),
                languages         = EXCLUDED.languages,
                topics            = EXCLUDED.topics,
                license_spdx      = COALESCE(EXCLUDED.license_spdx, repo.license_spdx),
                visibility        = COALESCE(EXCLUDED.visibility, repo.visibility),
                is_private        = EXCLUDED.is_private,
                is_fork           = EXCLUDED.is_fork,
                is_archived       = EXCLUDED.is_archived,
                is_template       = EXCLUDED.is_template,
                is_disabled       = EXCLUDED.is_disabled,
                disk_usage_kb     = EXCLUDED.disk_usage_kb,
                stargazers        = EXCLUDED.stargazers,
                watchers          = EXCLUDED.watchers,
                forks_count       = EXCLUDED.forks_count,
                open_issues       = EXCLUDED.open_issues,
                github_created_at = EXCLUDED.github_created_at,
                github_updated_at = EXCLUDED.github_updated_at,
                github_pushed_at  = EXCLUDED.github_pushed_at,
                -- COALESCE across the descriptive columns: a caller holding a
                -- partial record must not blank what discovery collected.
                raw_github        = CASE WHEN EXCLUDED.raw_github = '{}'::jsonb
                                         THEN repo.raw_github ELSE EXCLUDED.raw_github END,
                -- COALESCE, so a discovery run that carries no account context
                -- cannot strip attribution an earlier run established.
                account_id        = COALESCE(EXCLUDED.account_id, repo.account_id),
                updated_at        = now()
            RETURNING id
            """,
            {
                "github_id": record.github_id,
                "provider": record.provider,
                "host": record.host,
                "owner": record.owner,
                "name": record.name,
                "full_name": record.full_name,
                "description": record.description,
                "homepage": record.homepage,
                "html_url": record.html_url,
                "clone_url": record.clone_url,
                "ssh_url": record.ssh_url,
                "default_branch": record.default_branch,
                "primary_language": record.primary_language,
                "languages": json.dumps(record.languages),
                "topics": list(record.topics),
                "license_spdx": record.license_spdx,
                "visibility": record.visibility,
                "is_private": record.is_private,
                "is_fork": record.is_fork,
                "is_archived": record.is_archived,
                "is_template": record.is_template,
                "is_disabled": record.is_disabled,
                "disk_usage_kb": record.disk_usage_kb,
                "stargazers": record.stargazers,
                "watchers": record.watchers,
                "forks_count": record.forks_count,
                "open_issues": record.open_issues,
                "github_created_at": record.github_created_at,
                "github_updated_at": record.github_updated_at,
                "github_pushed_at": record.github_pushed_at,
                "raw_github": json.dumps(record.raw, default=str),
                "account_id": account_id,
            },
        ).fetchone()
        return int(row[0])

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def reserve_ids(conn: psycopg.Connection, sequence: str, count: int) -> list[int]:
    """Reserve ``count`` ids from a Postgres sequence in one round trip.

    Safe under concurrency: each caller gets a disjoint set of ids because
    ``nextval`` is atomic.
    """
    if count <= 0:
        return []
    rows = conn.execute(
        "SELECT nextval(%s) FROM generate_series(1, %s)", (sequence, count)
    ).fetchall()
    return [int(r[0]) for r in rows]


class AuthorCache:
    """Resolves author emails to ids, creating rows on demand.

    Emails are lowercased by the parser, so ``Alice@Corp.com`` and
    ``alice@corp.com`` collapse to one identity. Names vary far more than
    emails, so every distinct spelling is appended to ``known_names`` instead of
    overwriting.

    Why this uses its own connection
    --------------------------------
    ``author`` is the only table shared across repositories -- the same people
    commit to many of them. With several repos ingesting in parallel, each
    holding a long-running transaction, two workers that touch the same two
    authors in opposite orders deadlock, and Postgres kills one of them. That is
    not hypothetical: it failed 14 repositories on the first full run.

    Resolution therefore happens on a dedicated **autocommit** connection, so
    each author row is committed the instant it is written and no author lock is
    ever held across the surrounding ingest transaction. Author rows are
    independent reference data, so committing them outside the caller's
    transaction is safe: if the ingest later rolls back, the worst outcome is an
    author row with no commits yet pointing at it, which the next run reuses.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        # `conn` is kept only for its connection parameters.
        self._own = psycopg.connect(conninfo=get_config().db.dsn, autocommit=True)
        self._cache: dict[str, int] = {}

    def close(self) -> None:
        try:
            self._own.close()
        except Exception:
            log.debug("author connection already closed", exc_info=True)

    def resolve(self, email: str, name: str) -> int | None:
        """Return the author id for an email, inserting the author if new."""
        email = (email or "").strip().lower()
        if not email:
            return None
        cached = self._cache.get(email)
        if cached is not None:
            return cached

        # DO UPDATE (rather than DO NOTHING) so RETURNING yields a row on
        # conflict too. Each statement is its own transaction here, so the row
        # lock lives for microseconds.
        row = self._own.execute(
            """
            INSERT INTO author (email, display_name, known_names)
            VALUES (%s, %s, ARRAY[%s]::text[])
            ON CONFLICT (email) DO UPDATE SET
                display_name = COALESCE(author.display_name, EXCLUDED.display_name),
                known_names  = CASE
                    WHEN %s = ANY(author.known_names) THEN author.known_names
                    ELSE array_append(author.known_names, %s)
                END
            RETURNING id
            """,
            (email, name or None, name or "", name or "", name or ""),
        ).fetchone()
        author_id = int(row[0])
        self._cache[email] = author_id
        return author_id


class FileResolver:
    """Maps repo-relative paths to stable ``file`` ids, following renames.

    Holds the repo's entire path->id map in memory. Even a very large monorepo
    has on the order of 100k paths, so this is a few tens of MB and removes a
    database round trip from the innermost loop.
    """

    def __init__(self, conn: psycopg.Connection, repo_id: int) -> None:
        self._conn = conn
        self._repo_id = repo_id
        self._by_path: dict[str, int] = {}
        #: Ids created during this run, needing an INSERT on flush.
        self._pending_new: dict[int, str] = {}
        #: Renames applied this run: file_id -> newest path.
        self._pending_renames: dict[int, str] = {}
        #: Alias rows to write: (old_path, file_id).
        self._pending_aliases: list[tuple[str, int]] = []
        self._load()

    def _load(self) -> None:
        """Load current paths and all historical aliases for the repo."""
        for row in self._conn.execute(
            "SELECT id, path FROM file WHERE repo_id = %s", (self._repo_id,)
        ):
            self._by_path[row[1]] = int(row[0])
        for row in self._conn.execute(
            "SELECT old_path, file_id FROM file_alias WHERE repo_id = %s", (self._repo_id,)
        ):
            self._by_path.setdefault(row[0], int(row[1]))
        log.debug("repo %s: loaded %d known paths", self._repo_id, len(self._by_path))

    def resolve(self, path: str, old_path: str | None = None) -> int:
        """Return the file id for ``path``, honouring a rename from ``old_path``.

        When ``old_path`` is a path already seen, its existing id is carried
        forward to the new name so the file's history stays continuous.
        """
        if old_path:
            existing = self._by_path.get(old_path)
            if existing is not None:
                occupant = self._by_path.get(path)
                if occupant is not None and occupant != existing:
                    # Another file already holds the destination, so the rename
                    # cannot carry the identity there: the UPDATE in flush() is
                    # guarded against violating the unique index and would be
                    # skipped, leaving the map pointing at a row that still has
                    # the old path. Every later commit to this path then landed
                    # on the wrong file while the real one sat frozen -- 10,586
                    # rows across 55 repositories. Record against the occupant,
                    # which is the file that genuinely lives at this path.
                    return occupant
                # Carry the identity across the move.
                self._by_path[path] = existing
                self._pending_renames[existing] = path
                if old_path != path:
                    self._pending_aliases.append((old_path, existing))
                return existing

        existing = self._by_path.get(path)
        if existing is not None:
            return existing

        return self._allocate(path)

    def _allocate(self, path: str) -> int:
        """Reserve a fresh id for a path not seen before."""
        if not hasattr(self, "_id_block") or not self._id_block:
            self._id_block = reserve_ids(self._conn, "file_id_seq", 4096)
        file_id = self._id_block.pop()
        self._by_path[path] = file_id
        self._pending_new[file_id] = path
        return file_id

    def flush(self) -> int:
        """Write new files, rename updates and aliases. Returns new-file count."""
        created = len(self._pending_new)

        if self._pending_new:
            rows = []
            for file_id, path in self._pending_new.items():
                dir_path, basename, extension, depth = split_path(path)
                rows.append(
                    (file_id, self._repo_id, path, dir_path, basename, extension, depth)
                )
            self._conn.execute(
                """
                CREATE TEMP TABLE tmp_file (
                    id BIGINT, repo_id BIGINT, path TEXT, dir_path TEXT,
                    basename TEXT, extension TEXT, depth SMALLINT
                ) ON COMMIT DROP
                """
            )
            copy_rows(
                "tmp_file",
                ["id", "repo_id", "path", "dir_path", "basename", "extension", "depth"],
                rows,
                conn=self._conn,
            )
            self._conn.execute(
                """
                INSERT INTO file (id, repo_id, path, dir_path, basename, extension, depth)
                SELECT id, repo_id, path, dir_path, basename, extension, depth FROM tmp_file
                ON CONFLICT (repo_id, path) DO NOTHING
                """
            )
            self._conn.execute("DROP TABLE IF EXISTS tmp_file")
            self._pending_new.clear()

        if self._pending_renames:
            # Repoint each moved file at its newest name. A path collision can
            # occur when a file is moved onto a path that another row already
            # holds (git allows this across a delete); skip those rather than
            # violating the unique index, since the surviving row already
            # carries the history.
            for file_id, new_path in self._pending_renames.items():
                dir_path, basename, extension, depth = split_path(new_path)
                self._conn.execute(
                    """
                    UPDATE file SET path = %s, dir_path = %s, basename = %s,
                                    extension = %s, depth = %s
                    WHERE id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM file f2
                          WHERE f2.repo_id = file.repo_id AND f2.path = %s AND f2.id <> file.id
                      )
                    """,
                    (new_path, dir_path, basename, extension, depth, file_id, new_path),
                )
            self._pending_renames.clear()

        if self._pending_aliases:
            self._conn.execute(
                "CREATE TEMP TABLE tmp_alias (repo_id BIGINT, old_path TEXT, file_id BIGINT)"
                " ON COMMIT DROP"
            )
            copy_rows(
                "tmp_alias",
                ["repo_id", "old_path", "file_id"],
                ((self._repo_id, old, fid) for old, fid in self._pending_aliases),
                conn=self._conn,
            )
            self._conn.execute(
                """
                INSERT INTO file_alias (repo_id, old_path, file_id)
                SELECT DISTINCT ON (repo_id, old_path) repo_id, old_path, file_id
                FROM tmp_alias
                ON CONFLICT (repo_id, old_path) DO NOTHING
                """
            )
            self._conn.execute("DROP TABLE IF EXISTS tmp_alias")
            self._pending_aliases.clear()

        return created


@dataclass
class LoadStats:
    """Counters describing one repository load."""

    commits_read: int = 0
    commits_written: int = 0
    files_created: int = 0
    changes_written: int = 0
    pair_eligible: int = 0
    skipped_oversized: int = 0
    newest_sha: str | None = None


def load_commits(
    repo_id: int,
    commits: Iterable[ParsedCommit],
    conn: psycopg.Connection,
    max_files_per_commit: int | None = None,
) -> LoadStats:
    """Persist a stream of parsed commits for one repository.

    Args:
        repo_id: target repository.
        commits: parsed commits in **oldest-first** order. Rename tracking
            depends on this ordering.
        conn: an open connection; the caller owns the transaction.
        max_files_per_commit: commits touching more files than this are stored
            in full but flagged ``pair_eligible = FALSE``, excluding them from
            co-occurrence counting.

    Returns:
        A :class:`LoadStats` describing what was written.
    """
    cfg = get_config().ingest
    cap = max_files_per_commit if max_files_per_commit is not None else cfg.max_files_per_commit

    authors = AuthorCache(conn)
    files = FileResolver(conn, repo_id)
    stats = LoadStats()

    # Cleared in place rather than rebound, so every closure below keeps
    # referring to the same list objects.
    commit_rows: list[tuple] = []
    change_rows: list[tuple] = []
    parent_rows: list[tuple] = []
    id_block: list[int] = []

    def next_commit_id() -> int:
        if not id_block:
            id_block.extend(reserve_ids(conn, "commit_id_seq", COMMIT_FLUSH_SIZE))
        return id_block.pop()

    def flush() -> None:
        if not commit_rows:
            return
        stats.files_created += files.flush()
        # Report what the database actually accepted. ON CONFLICT DO NOTHING
        # silently drops commits already present from an earlier run, so
        # counting staged rows would overstate the work on every re-ingest.
        inserted_commits, inserted_changes = _write_commit_batch(
            conn, commit_rows, change_rows, parent_rows
        )
        stats.commits_written += inserted_commits
        stats.changes_written += inserted_changes
        commit_rows.clear()
        change_rows.clear()
        parent_rows.clear()

    try:
        for parsed in commits:
            stats.commits_read += 1
            stats.newest_sha = parsed.sha

            n_files = len(parsed.files)
            oversized = cap > 0 and n_files > cap
            eligible = (not parsed.is_merge) and n_files > 0 and not oversized
            if oversized:
                stats.skipped_oversized += 1
            if eligible:
                stats.pair_eligible += 1

            commit_id = next_commit_id()
            commit_rows.append(
                (
                    commit_id,
                    repo_id,
                    parsed.sha,
                    authors.resolve(parsed.author_email, parsed.author_name),
                    authors.resolve(parsed.committer_email, parsed.committer_name),
                    parsed.authored_at,
                    parsed.committed_at,
                    parsed.subject[:2000],
                    parsed.body[:20000] if parsed.body else None,
                    len(parsed.parents),
                    parsed.is_merge,
                    n_files,
                    parsed.insertions,
                    parsed.deletions,
                    eligible,
                )
            )

            for ordinal, parent_sha in enumerate(parsed.parents):
                parent_rows.append((repo_id, parsed.sha, parent_sha, ordinal))

            # A commit can list the same path twice (a rename plus a
            # modification collapsing onto one target). The primary key on
            # (commit_id, file_id) forbids duplicates, so keep the first.
            seen: set[int] = set()
            for change in parsed.files:
                file_id = files.resolve(change.path, change.old_path)
                if file_id in seen:
                    continue
                seen.add(file_id)
                change_rows.append(
                    (
                        commit_id,
                        file_id,
                        repo_id,
                        change.change_type,
                        change.insertions,
                        change.deletions,
                        change.is_binary,
                        change.old_path,
                        change.similarity,
                    )
                )

            if len(commit_rows) >= COMMIT_FLUSH_SIZE:
                flush()

        flush()
    finally:
        authors.close()

    return stats


def _write_commit_batch(
    conn: psycopg.Connection,
    commit_rows: list[tuple],
    change_rows: list[tuple],
    parent_rows: list[tuple],
) -> tuple[int, int]:
    """COPY a batch into temp tables, then merge idempotently.

    Returns:
        ``(commits_inserted, changes_inserted)`` -- the counts the database
        actually accepted, which is lower than the staged count whenever a
        commit was already present.
    """
    conn.execute(
        """
        CREATE TEMP TABLE tmp_commit (
            id BIGINT, repo_id BIGINT, sha TEXT, author_id BIGINT, committer_id BIGINT,
            authored_at TIMESTAMPTZ, committed_at TIMESTAMPTZ, subject TEXT, body TEXT,
            parent_count SMALLINT, is_merge BOOLEAN, n_files INTEGER,
            insertions INTEGER, deletions INTEGER, pair_eligible BOOLEAN
        ) ON COMMIT DROP
        """
    )
    copy_rows(
        "tmp_commit",
        [
            "id", "repo_id", "sha", "author_id", "committer_id", "authored_at",
            "committed_at", "subject", "body", "parent_count", "is_merge",
            "n_files", "insertions", "deletions", "pair_eligible",
        ],
        commit_rows,
        conn=conn,
    )
    inserted = conn.execute(
        """
        INSERT INTO commit (
            id, repo_id, sha, author_id, committer_id, authored_at, committed_at,
            subject, body, parent_count, is_merge, n_files, insertions, deletions,
            pair_eligible
        )
        SELECT id, repo_id, sha, author_id, committer_id, authored_at, committed_at,
               subject, body, parent_count, is_merge, n_files, insertions, deletions,
               pair_eligible
        FROM tmp_commit
        ON CONFLICT (repo_id, sha) DO NOTHING
        """
    ).rowcount
    commits_inserted = int(inserted or 0)
    changes_inserted = 0

    if change_rows:
        conn.execute(
            """
            CREATE TEMP TABLE tmp_change (
                commit_id BIGINT, file_id BIGINT, repo_id BIGINT, change_type CHAR(1),
                insertions INTEGER, deletions INTEGER, is_binary BOOLEAN,
                old_path TEXT, similarity SMALLINT
            ) ON COMMIT DROP
            """
        )
        copy_rows(
            "tmp_change",
            [
                "commit_id", "file_id", "repo_id", "change_type", "insertions",
                "deletions", "is_binary", "old_path", "similarity",
            ],
            change_rows,
            conn=conn,
        )
        # Join back through `commit` so a change whose commit lost the
        # ON CONFLICT race (already present from an earlier run) is dropped
        # rather than orphaned against a non-existent commit id.
        changed = conn.execute(
            """
            INSERT INTO commit_file (
                commit_id, file_id, repo_id, change_type, insertions, deletions,
                is_binary, old_path, similarity
            )
            SELECT t.commit_id, t.file_id, t.repo_id, t.change_type, t.insertions,
                   t.deletions, t.is_binary, t.old_path, t.similarity
            FROM tmp_change t
            JOIN commit c ON c.id = t.commit_id
            ON CONFLICT (commit_id, file_id) DO NOTHING
            """
        ).rowcount
        changes_inserted = int(changed or 0)
        conn.execute("DROP TABLE IF EXISTS tmp_change")

    if parent_rows:
        conn.execute(
            "CREATE TEMP TABLE tmp_parent (repo_id BIGINT, child_sha TEXT,"
            " parent_sha TEXT, ordinal SMALLINT) ON COMMIT DROP"
        )
        copy_rows(
            "tmp_parent",
            ["repo_id", "child_sha", "parent_sha", "ordinal"],
            parent_rows,
            conn=conn,
        )
        conn.execute(
            """
            INSERT INTO commit_parent (repo_id, child_sha, parent_sha, ordinal)
            SELECT DISTINCT ON (repo_id, child_sha, parent_sha)
                   repo_id, child_sha, parent_sha, ordinal
            FROM tmp_parent
            ON CONFLICT DO NOTHING
            """
        )
        conn.execute("DROP TABLE IF EXISTS tmp_parent")

    conn.execute("DROP TABLE IF EXISTS tmp_commit")
    return commits_inserted, changes_inserted
