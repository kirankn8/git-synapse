"""Derivation of aggregate tables from the atomic ``commit_file`` facts.

Nothing here holds information that the fact tables do not already contain.
Every table this module writes -- marginals, file pairs, directory pairs,
author affinity -- is a materialised cache that can be dropped and rebuilt at
any time with :func:`rebuild_repo`, which is what makes it safe to change the
fan-out cap, the support threshold or the recency half-life after the fact.

The population question
-----------------------
A contingency table needs a population size ``N``, and choosing it wrongly is
the easiest way to get every measure subtly wrong. ``N`` here is
``repo.pair_population``: the number of commits in the repository that were
*eligible to contribute a pair* -- non-merge, non-empty, and under the fan-out
cap. It is deliberately not ``repo.commit_count``.

The marginals must come from the same population, so ``file.pair_change_count``
counts only pair-eligible commits too, while ``file.change_count`` keeps the
true total for display. Mixing the two would produce impossible tables where
``n_ab > n_a``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import psycopg

from git_synapse.config import get_config
from git_synapse.db.engine import connection

log = logging.getLogger(__name__)


@dataclass
class AggregateStats:
    """Row counts produced by one repository's aggregation."""

    repo_id: int
    files: int = 0
    directories: int = 0
    file_pairs: int = 0
    dir_pairs: int = 0
    author_files: int = 0
    pair_population: int = 0
    duration_s: float = 0.0


def rebuild_repo(repo_id: int, conn: psycopg.Connection | None = None) -> AggregateStats:
    """Recompute every derived table for one repository.

    Scoped per repository so that repositories aggregate independently and in
    parallel, and so a daily refresh only touches the ones that actually moved.
    """

    def _run(c: psycopg.Connection) -> AggregateStats:
        started = time.monotonic()
        stats = AggregateStats(repo_id=repo_id)

        _refresh_commit_flags(c, repo_id)
        stats.pair_population = _refresh_repo_population(c, repo_id)
        stats.files = _refresh_file_marginals(c, repo_id)
        stats.directories = _refresh_directories(c, repo_id)
        stats.file_pairs = _rebuild_file_pairs(c, repo_id)
        stats.dir_pairs = _rebuild_dir_pairs(c, repo_id)
        stats.author_files = _rebuild_author_files(c, repo_id)
        _refresh_repo_summary(c, repo_id)

        stats.duration_s = time.monotonic() - started
        log.info(
            "repo %s aggregated in %.1fs: %d files, %d dirs, %d file pairs, %d dir pairs",
            repo_id, stats.duration_s, stats.files, stats.directories,
            stats.file_pairs, stats.dir_pairs,
        )
        return stats

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def _refresh_commit_flags(conn: psycopg.Connection, repo_id: int) -> None:
    """Recompute ``commit.pair_eligible`` against the current configuration.

    Stored rather than applied at query time so the exclusion is auditable, and
    recomputed here so that changing ``MAX_FILES_PER_COMMIT`` takes effect on the
    next aggregation without re-reading git.
    """
    cfg = get_config().ingest
    conn.execute(
        """
        UPDATE commit SET pair_eligible = (
            NOT is_merge
            AND n_files > 0
            AND (%(cap)s <= 0 OR n_files <= %(cap)s)
        )
        WHERE repo_id = %(repo)s
          AND pair_eligible <> (
              NOT is_merge
              AND n_files > 0
              AND (%(cap)s <= 0 OR n_files <= %(cap)s)
          )
        """,
        {"repo": repo_id, "cap": cfg.max_files_per_commit},
    )


def _refresh_repo_population(conn: psycopg.Connection, repo_id: int) -> int:
    """Set ``repo.pair_population`` -- the ``N`` of every contingency table."""
    row = conn.execute(
        """
        UPDATE repo SET pair_population = sub.n
        FROM (
            SELECT count(*) AS n FROM commit
            WHERE repo_id = %(repo)s AND pair_eligible
        ) sub
        WHERE repo.id = %(repo)s
        RETURNING repo.pair_population
        """,
        {"repo": repo_id},
    ).fetchone()
    return int(row[0]) if row else 0


def _refresh_file_marginals(conn: psycopg.Connection, repo_id: int) -> int:
    """Recompute per-file counters from ``commit_file``.

    ``pair_change_count`` is restricted to pair-eligible commits so it lines up
    with the joint counts in ``file_pair``; ``change_count`` keeps the honest
    total for display.
    """
    row = conn.execute(
        """
        WITH agg AS (
            SELECT cf.file_id,
                   count(*)                                          AS change_count,
                   count(*) FILTER (WHERE c.pair_eligible)           AS pair_change_count,
                   sum(cf.insertions)                                AS insertions,
                   sum(cf.deletions)                                 AS deletions,
                   count(DISTINCT c.author_id)                       AS author_count,
                   min(c.committed_at)                               AS first_change_at,
                   max(c.committed_at)                               AS last_change_at
            FROM commit_file cf
            JOIN commit c ON c.id = cf.commit_id
            WHERE cf.repo_id = %(repo)s
            GROUP BY cf.file_id
        )
        UPDATE file f SET
            change_count      = agg.change_count,
            pair_change_count = agg.pair_change_count,
            insertions        = COALESCE(agg.insertions, 0),
            deletions         = COALESCE(agg.deletions, 0),
            author_count      = agg.author_count,
            first_change_at   = agg.first_change_at,
            last_change_at    = agg.last_change_at
        FROM agg
        WHERE f.id = agg.file_id
        """,
        {"repo": repo_id},
    )
    # Mark files whose most recent change was a delete, so the UI can grey them out.
    conn.execute(
        """
        UPDATE file f SET is_deleted = latest.change_type = 'D'
        FROM (
            SELECT DISTINCT ON (cf.file_id) cf.file_id, cf.change_type
            FROM commit_file cf
            JOIN commit c ON c.id = cf.commit_id
            WHERE cf.repo_id = %(repo)s
            ORDER BY cf.file_id, c.committed_at DESC, c.id DESC
        ) latest
        WHERE f.id = latest.file_id
        """,
        {"repo": repo_id},
    )
    count = conn.execute(
        "SELECT count(*) FROM file WHERE repo_id = %s", (repo_id,)
    ).fetchone()
    return int(count[0])


def _refresh_directories(conn: psycopg.Connection, repo_id: int) -> int:
    """Materialise the directory tree and the file->ancestor mapping.

    Ancestors are generated in SQL by splitting each file's ``dir_path`` on
    ``/`` and re-joining successive prefixes. The empty string is included so
    the repository root participates as a directory in its own right.
    """
    conn.execute(
        """
        WITH parts AS (
            SELECT f.id AS file_id, f.dir_path,
                   string_to_array(NULLIF(f.dir_path, ''), '/') AS segs
            FROM file f
            WHERE f.repo_id = %(repo)s
        ),
        ancestors AS (
            -- The repository root, which every file belongs to.
            SELECT file_id, '' AS path FROM parts
            UNION
            SELECT p.file_id, array_to_string(p.segs[1:i], '/') AS path
            FROM parts p
            CROSS JOIN LATERAL generate_series(1, COALESCE(array_length(p.segs, 1), 0)) AS i
        )
        INSERT INTO directory (repo_id, path, depth)
        SELECT DISTINCT %(repo)s, path,
               CASE WHEN path = '' THEN 0 ELSE array_length(string_to_array(path, '/'), 1) END
        FROM ancestors
        ON CONFLICT (repo_id, path) DO NOTHING
        """,
        {"repo": repo_id},
    )

    conn.execute("DELETE FROM file_directory WHERE repo_id = %s", (repo_id,))
    conn.execute(
        """
        WITH parts AS (
            SELECT f.id AS file_id, string_to_array(NULLIF(f.dir_path, ''), '/') AS segs
            FROM file f WHERE f.repo_id = %(repo)s
        ),
        ancestors AS (
            SELECT file_id, '' AS path FROM parts
            UNION
            SELECT p.file_id, array_to_string(p.segs[1:i], '/')
            FROM parts p
            CROSS JOIN LATERAL generate_series(1, COALESCE(array_length(p.segs, 1), 0)) AS i
        )
        INSERT INTO file_directory (repo_id, file_id, dir_id)
        SELECT %(repo)s, a.file_id, d.id
        FROM ancestors a
        JOIN directory d ON d.repo_id = %(repo)s AND d.path = a.path
        ON CONFLICT DO NOTHING
        """,
        {"repo": repo_id},
    )

    # Directory marginals, computed independently of the file level: a
    # directory changes in a commit if ANY file beneath it changed, so these
    # cannot be summed up from file counts.
    conn.execute(
        """
        WITH dir_commits AS (
            SELECT DISTINCT fd.dir_id, c.id AS commit_id, c.pair_eligible, c.committed_at
            FROM commit_file cf
            JOIN commit c ON c.id = cf.commit_id
            JOIN file_directory fd ON fd.file_id = cf.file_id
            WHERE cf.repo_id = %(repo)s
        ),
        agg AS (
            SELECT dir_id,
                   count(*)                                AS change_count,
                   count(*) FILTER (WHERE pair_eligible)   AS pair_change_count,
                   min(committed_at)                       AS first_change_at,
                   max(committed_at)                       AS last_change_at
            FROM dir_commits GROUP BY dir_id
        )
        UPDATE directory d SET
            change_count      = agg.change_count,
            pair_change_count = agg.pair_change_count,
            first_change_at   = agg.first_change_at,
            last_change_at    = agg.last_change_at
        FROM agg WHERE d.id = agg.dir_id
        """,
        {"repo": repo_id},
    )
    conn.execute(
        """
        UPDATE directory d SET file_count = sub.n
        FROM (SELECT dir_id, count(*) n FROM file_directory
              WHERE repo_id = %(repo)s GROUP BY dir_id) sub
        WHERE d.id = sub.dir_id
        """,
        {"repo": repo_id},
    )
    row = conn.execute(
        "SELECT count(*) FROM directory WHERE repo_id = %s", (repo_id,)
    ).fetchone()
    return int(row[0])


def _rebuild_file_pairs(conn: psycopg.Connection, repo_id: int) -> int:
    """Regenerate ``file_pair`` for one repository.

    The pair set is produced by a self-join of ``commit_file`` on ``commit_id``
    with ``file_a_id < file_b_id``, which emits each unordered pair exactly once
    and costs O(k^2) rows for a commit touching k files. The fan-out cap is what
    keeps that quadratic term bounded.

    ``w_ab`` accumulates an exponential recency weight per co-change, so a
    caller can rank by "coupled *lately*" without a second pass over history.
    """
    cfg = get_config()
    half_life = max(cfg.analysis.recency_half_life_days, 1)
    min_support = max(cfg.ingest.min_pair_support, 1)

    conn.execute("DELETE FROM file_pair WHERE repo_id = %s", (repo_id,))
    row = conn.execute(
        """
        INSERT INTO file_pair (
            repo_id, file_a_id, file_b_id, n_ab, w_ab,
            first_co_change, last_co_change, distinct_authors
        )
        SELECT
            %(repo)s,
            a.file_id,
            b.file_id,
            count(*),
            sum(power(0.5, EXTRACT(EPOCH FROM (now() - c.committed_at)) / 86400.0
                            / %(half_life)s)),
            min(c.committed_at),
            max(c.committed_at),
            count(DISTINCT c.author_id)
        FROM commit_file a
        JOIN commit_file b
          ON b.commit_id = a.commit_id
         AND b.file_id > a.file_id
        JOIN commit c ON c.id = a.commit_id
        WHERE a.repo_id = %(repo)s
          AND c.pair_eligible
        GROUP BY a.file_id, b.file_id
        HAVING count(*) >= %(min_support)s
        """,
        {"repo": repo_id, "half_life": half_life, "min_support": min_support},
    ).rowcount
    return int(row or 0)


def _rebuild_dir_pairs(conn: psycopg.Connection, repo_id: int) -> int:
    """Regenerate ``dir_pair``: the same co-change maths one level up the tree.

    A directory participates in a commit if any file beneath it changed, so the
    commit-to-directory relation is de-duplicated before pairing. Without the
    DISTINCT, a commit touching ten files in one directory would count that
    directory ten times.
    """
    cfg = get_config()
    half_life = max(cfg.analysis.recency_half_life_days, 1)
    min_support = max(cfg.ingest.min_pair_support, 1)

    conn.execute("DELETE FROM dir_pair WHERE repo_id = %s", (repo_id,))
    row = conn.execute(
        """
        WITH dir_commits AS (
            SELECT DISTINCT fd.dir_id, c.id AS commit_id, c.committed_at
            FROM commit_file cf
            JOIN commit c ON c.id = cf.commit_id AND c.pair_eligible
            JOIN file_directory fd ON fd.file_id = cf.file_id
            WHERE cf.repo_id = %(repo)s
        )
        INSERT INTO dir_pair (repo_id, dir_a_id, dir_b_id, n_ab, w_ab,
                              first_co_change, last_co_change)
        SELECT %(repo)s, a.dir_id, b.dir_id, count(*),
               sum(power(0.5, EXTRACT(EPOCH FROM (now() - a.committed_at)) / 86400.0
                               / %(half_life)s)),
               min(a.committed_at), max(a.committed_at)
        FROM dir_commits a
        JOIN dir_commits b ON b.commit_id = a.commit_id AND b.dir_id > a.dir_id
        GROUP BY a.dir_id, b.dir_id
        HAVING count(*) >= %(min_support)s
        """,
        {"repo": repo_id, "half_life": half_life, "min_support": min_support},
    ).rowcount
    return int(row or 0)


def _rebuild_author_files(conn: psycopg.Connection, repo_id: int) -> int:
    """Regenerate author-to-file affinity: who actually works on what."""
    conn.execute("DELETE FROM author_file WHERE repo_id = %s", (repo_id,))
    row = conn.execute(
        """
        INSERT INTO author_file (repo_id, author_id, file_id, n_commits,
                                 insertions, deletions, first_at, last_at)
        SELECT %(repo)s, c.author_id, cf.file_id, count(*),
               sum(cf.insertions), sum(cf.deletions),
               min(c.committed_at), max(c.committed_at)
        FROM commit_file cf
        JOIN commit c ON c.id = cf.commit_id
        WHERE cf.repo_id = %(repo)s AND c.author_id IS NOT NULL
        GROUP BY c.author_id, cf.file_id
        """,
        {"repo": repo_id},
    ).rowcount
    return int(row or 0)


def _refresh_repo_summary(conn: psycopg.Connection, repo_id: int) -> None:
    """Refresh the denormalised counters shown on the repository list."""
    conn.execute(
        """
        UPDATE repo r SET
            commit_count     = COALESCE(s.commits, 0),
            file_count       = COALESCE(s.files, 0),
            author_count     = COALESCE(s.authors, 0),
            pair_count       = COALESCE(p.pairs, 0),
            total_insertions = COALESCE(s.ins, 0),
            total_deletions  = COALESCE(s.dels, 0),
            first_commit_at  = s.first_at,
            last_commit_at   = s.last_at,
            last_aggregate_at = now()
        FROM
            (SELECT count(*) AS commits,
                    count(DISTINCT author_id) AS authors,
                    sum(insertions) AS ins,
                    sum(deletions) AS dels,
                    min(committed_at) AS first_at,
                    max(committed_at) AS last_at,
                    (SELECT count(*) FROM file WHERE repo_id = %(repo)s) AS files
             FROM commit WHERE repo_id = %(repo)s) s,
            (SELECT count(*) AS pairs FROM file_pair WHERE repo_id = %(repo)s) p
        WHERE r.id = %(repo)s
        """,
        {"repo": repo_id},
    )


def repos_needing_aggregation(conn: psycopg.Connection | None = None) -> list[int]:
    """Repositories whose atomic data has changed since their last aggregation."""

    def _run(c: psycopg.Connection) -> list[int]:
        rows = c.execute(
            """
            SELECT id FROM repo
            WHERE is_enabled
              AND (last_aggregate_at IS NULL
                   OR last_ingest_at IS NULL
                   OR last_aggregate_at < last_ingest_at)
            ORDER BY id
            """
        ).fetchall()
        return [int(r[0]) for r in rows]

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)
