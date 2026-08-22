"""Cross-repository coupling: change sets, repo pairs, and cross-repo file pairs.

The problem
-----------
:mod:`git_synapse.analysis.aggregate` defines "changed together" as "appeared in the
same commit". Two repositories never share a commit, so that definition cannot
express ``signer -> packager -> runtime``.

The unit of work therefore widens from the *commit* to the **change set**: a
group of commits, possibly spanning repositories, that constitute one logical
change. Once change sets exist, cross-repo coupling is the *same* computation as
within-repo coupling with the transaction swapped -- so this module reuses
:class:`~git_synapse.stats.contingency.Contingency` and the whole measure registry
unchanged, and produces the same 31 columns.

Forming change sets
-------------------
Every pair-eligible commit lands in exactly one change set, which makes the
change sets a partition and the population size ``N`` unambiguous:

1. **ticket** -- the subject carries an issue key (``ACME-2330``). All commits
   sharing that key form one change set. Precise, and 1,709 keys already span
   more than one repository in this org, but coverage is uneven: ``telemetry`` is
   37% keyed while ``signer`` is 0%.
2. **temporal** -- otherwise, consecutive commits by one author with no gap
   longer than ``session_gap_hours`` form a work session. This is what reaches
   repositories with no commit-message convention, at the cost of noise from
   unrelated same-afternoon work.

Single-repo change sets are kept on purpose. See the schema comment: dropping
them would empty the ``b`` and ``c`` cells of every contingency table and push
every score toward 1.0.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
import psycopg

from git_synapse.config import get_config
from git_synapse.db.engine import connection, copy_rows
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import ALL_KEYS, BY_KEY

log = logging.getLogger(__name__)

#: Column order shared by both cross-repo metric tables, after the key columns.
_CELLS = ("n_ab", "n_a", "n_b", "n_total")

#: Rows per vectorised scoring batch.
_SCORE_BATCH = 200_000


@dataclass
class CrossRepoStats:
    """Row counts produced by one cross-repo build."""

    change_sets: int = 0
    ticket_sets: int = 0
    temporal_sets: int = 0
    eligible_sets: int = 0
    repo_pairs: int = 0
    file_pairs: int = 0
    duration_s: float = 0.0


def rebuild(
    conn: psycopg.Connection | None = None, force: bool = False
) -> CrossRepoStats:
    """Rebuild the cross-repo tables from the atomic commit facts.

    Global rather than per-repo, because a change set spans repositories by
    definition. Runs as a single pass after all per-repo ingestion completes.

    Args:
        conn: reuse an open connection.
        force: rebuild every change set from scratch. Without this, only the
            tickets and authors touched by newly-ingested commits are
            re-partitioned -- see :func:`_build_change_sets` for why that is
            sound.

    Note on what stays a full recompute: once any change set is added, the
    population ``N`` shifts, and ``N`` appears in every pair's contingency table.
    Marginals and scores are therefore recomputed globally even on an
    incremental run. That is deliberate -- delta-merging them would let the
    stored values drift from the true ones -- and cheap, because the scoring pass
    is vectorised.
    """

    def _run(c: psycopg.Connection) -> CrossRepoStats:
        started = time.monotonic()
        stats = CrossRepoStats()

        if not _build_change_sets(c, stats, force=force):
            log.info("cross-repo: no new commits to partition; nothing to do")
            _collect_counts(c, stats)
            stats.duration_s = time.monotonic() - started
            return stats
        _mark_eligibility(c, stats)
        _refresh_marginals(c)
        stats.repo_pairs = _build_repo_pairs(c)
        stats.file_pairs = _build_file_pairs(c)
        _score(c, level="repo")
        _score(c, level="file")

        stats.duration_s = time.monotonic() - started
        log.info(
            "cross-repo rebuilt in %.1fs: %d change sets (%d ticket, %d temporal, "
            "%d eligible), %d repo pairs, %d file pairs",
            stats.duration_s, stats.change_sets, stats.ticket_sets,
            stats.temporal_sets, stats.eligible_sets, stats.repo_pairs,
            stats.file_pairs,
        )
        return stats

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def _build_change_sets(
    conn: psycopg.Connection, stats: CrossRepoStats, force: bool = False
) -> bool:
    """Partition pair-eligible commits into change sets.

    Why this can be incremental
    ---------------------------
    A change set is keyed either by ticket (``ticket:ACME-1234``) or by author
    session (``session:<author>:<n>``). Both keys are *local*: a new commit can
    only affect the change set of its own ticket, or the sessions of its own
    author. No other change set can be disturbed. So an incremental pass need
    only re-partition the tickets and authors that newly-ingested commits touch.

    Author sessions are recomputed from that author's **entire** history rather
    than appended to, because a commit can bridge a gap and merge two previously
    separate sessions. Per author that is a bounded amount of work.

    Returns:
        True if anything was rebuilt, False if there was nothing new.
    """
    cfg = get_config().crossrepo

    if force:
        # DELETE rather than TRUNCATE: TRUNCATE takes AccessExclusive, which lets a
        # concurrent reader invert lock order and deadlock the rebuild -- and the
        # rebuild is always the victim, so it rolls back while the run still
        # reports success. These tables are small enough that the cost is noise.
        conn.execute("DELETE FROM change_set_commit")
        conn.execute("DELETE FROM change_set")
        scope_sql = "c.pair_eligible"
        scope_params: dict = {}
    else:
        # Commits that are pair-eligible, not yet in any change set, and
        # *assignable*: a change set is keyed by ticket or by author, so a commit
        # with neither can never join one. 73 such commits exist in this corpus
        # (author identity missing from the git object), and counting them as
        # "new work" made the early return unreachable -- every run then did a
        # full re-partition even when nothing had changed.
        rows = conn.execute(
            """
            SELECT DISTINCT
                   (regexp_match(c.subject, %(pattern)s))[1] AS ticket,
                   c.author_id
            FROM commit c
            WHERE c.pair_eligible
              AND (c.author_id IS NOT NULL OR c.subject ~ %(pattern)s)
              AND NOT EXISTS (SELECT 1 FROM change_set_commit x
                               WHERE x.commit_id = c.id)
            """,
            {"pattern": cfg.ticket_pattern},
        ).fetchall()
        if not rows:
            return False

        tickets = sorted({r[0] for r in rows if r[0]})
        authors = sorted({int(r[1]) for r in rows if r[1] is not None})
        log.info(
            "cross-repo incremental: %d ticket(s), %d author session group(s) affected",
            len(tickets), len(authors),
        )

        # Drop only the affected change sets; change_set_commit cascades.
        conn.execute(
            """
            DELETE FROM change_set
            WHERE (ticket IS NOT NULL AND ticket = ANY(%(tickets)s))
               OR (signal = 'temporal' AND author_id = ANY(%(authors)s))
            """,
            {"tickets": tickets, "authors": authors},
        )
        # Re-partition exactly the commits those change sets covered, which is
        # every commit belonging to an affected ticket or author.
        scope_sql = (
            "c.pair_eligible"
            " AND (c.author_id IS NOT NULL OR c.subject ~ %(pattern2)s)"
            " AND ("
            " (c.subject ~ %(pattern2)s"
            "   AND (regexp_match(c.subject, %(pattern2)s))[1] = ANY(%(tickets)s))"
            " OR c.author_id = ANY(%(authors)s)"
            " OR NOT EXISTS (SELECT 1 FROM change_set_commit x WHERE x.commit_id = c.id)"
            ")"
        )
        scope_params = {"pattern2": cfg.ticket_pattern, "tickets": tickets,
                        "authors": authors}

    conn.execute(
        """
        CREATE TEMP TABLE tmp_assign (
            commit_id BIGINT PRIMARY KEY,
            repo_id   BIGINT NOT NULL,
            key       TEXT   NOT NULL,
            signal    TEXT   NOT NULL,
            ticket    TEXT,
            author_id BIGINT
        ) ON COMMIT DROP
        """
    )

    # --- 1. ticket-keyed change sets -------------------------------------
    conn.execute(
        f"""
        INSERT INTO tmp_assign (commit_id, repo_id, key, signal, ticket, author_id)
        SELECT c.id, c.repo_id,
               'ticket:' || (regexp_match(c.subject, %(pattern)s))[1],
               'ticket',
               (regexp_match(c.subject, %(pattern)s))[1],
               c.author_id
        FROM commit c
        WHERE {scope_sql}
          AND c.subject ~ %(pattern)s
        """,
        {"pattern": cfg.ticket_pattern, **scope_params},
    )

    # --- 2. temporal sessions for everything else ------------------------
    # Sessions are derived from the author's whole history (no scope filter on
    # the window function), then restricted to the commits in scope, so a
    # session that spans the incremental boundary still gets one consistent id.
    conn.execute(
        f"""
        INSERT INTO tmp_assign (commit_id, repo_id, key, signal, ticket, author_id)
        WITH candidates AS (
            SELECT c.id, c.repo_id, c.author_id, c.committed_at,
                   ({scope_sql}) AS in_scope
            FROM commit c
            WHERE c.pair_eligible
              AND c.author_id IS NOT NULL
              AND c.subject !~ %(pattern)s
        ),
        gaps AS (
            SELECT *,
                   CASE
                       WHEN lag(committed_at) OVER w IS NULL
                         OR committed_at - lag(committed_at) OVER w
                            > make_interval(hours => %(gap)s)
                       THEN 1 ELSE 0
                   END AS opens_session
            FROM candidates
            WINDOW w AS (PARTITION BY author_id ORDER BY committed_at)
        ),
        sessions AS (
            SELECT id, repo_id, author_id, in_scope,
                   sum(opens_session) OVER (
                       PARTITION BY author_id ORDER BY committed_at
                       ROWS UNBOUNDED PRECEDING
                   ) AS session_no
            FROM gaps
        )
        SELECT id, repo_id,
               'session:' || author_id || ':' || session_no,
               'temporal', NULL, author_id
        FROM sessions
        WHERE in_scope
          AND NOT EXISTS (SELECT 1 FROM tmp_assign t WHERE t.commit_id = sessions.id)
        """,
        {"pattern": cfg.ticket_pattern, "gap": cfg.session_gap_hours, **scope_params},
    )

    # Both the grouping below and the membership join read `key`.
    conn.execute("CREATE INDEX ON tmp_assign (key)")
    conn.execute("ANALYZE tmp_assign")

    conn.execute(
        """
        INSERT INTO change_set (key, signal, ticket, author_id, n_commits,
                                n_repos, first_at, last_at)
        SELECT a.key,
               min(a.signal),
               min(a.ticket),
               -- A ticket can involve several people; attribute it to whoever
               -- contributed the most commits. `mode()` is a single-pass
               -- ordered-set aggregate: a correlated subquery here re-scanned
               -- the 250k-row staging table once per group and did not finish
               -- in ten minutes.
               mode() WITHIN GROUP (ORDER BY a.author_id),
               count(*),
               count(DISTINCT a.repo_id),
               min(c.committed_at),
               max(c.committed_at)
        FROM tmp_assign a
        JOIN commit c ON c.id = a.commit_id
        GROUP BY a.key
        ON CONFLICT (key) DO UPDATE SET
            n_commits = EXCLUDED.n_commits,
            n_repos   = EXCLUDED.n_repos,
            first_at  = EXCLUDED.first_at,
            last_at   = EXCLUDED.last_at,
            author_id = EXCLUDED.author_id
        """
    )
    conn.execute(
        """
        INSERT INTO change_set_commit (change_set_id, commit_id, repo_id)
        SELECT cs.id, a.commit_id, a.repo_id
        FROM tmp_assign a JOIN change_set cs ON cs.key = a.key
        ON CONFLICT DO NOTHING
        """
    )
    # Counters must come from actual membership, not from the slice this pass
    # happened to see. An incremental run scopes in every commit by an affected
    # author, including their commits under tickets that were NOT re-partitioned,
    # so the upsert above overwrites those sets' counts with a fragment of
    # themselves. n_repos then gates cross-repo pairing, so a fragment both
    # dropped real pairs and let sprawling sets past the fan-out cap.
    conn.execute(
        """
        UPDATE change_set cs
           SET n_commits = m.n_commits, n_repos = m.n_repos,
               first_at = m.first_at, last_at = m.last_at
        FROM (
            SELECT csc.change_set_id, count(*) AS n_commits,
                   count(DISTINCT csc.repo_id) AS n_repos,
                   min(c.committed_at) AS first_at, max(c.committed_at) AS last_at
            FROM change_set_commit csc
            JOIN commit c ON c.id = csc.commit_id
            GROUP BY csc.change_set_id
        ) m
        WHERE m.change_set_id = cs.id
          AND (cs.n_commits, cs.n_repos, cs.first_at, cs.last_at)
              IS DISTINCT FROM (m.n_commits, m.n_repos, m.first_at, m.last_at)
        """
    )
    # A set whose commits all went away is not eligible for anything, and left in
    # place it keeps inflating N -- the denominator of every cross-repo measure.
    conn.execute(
        """
        DELETE FROM change_set cs
        WHERE NOT EXISTS (
            SELECT 1 FROM change_set_commit x WHERE x.change_set_id = cs.id
        )
        """
    )
    conn.execute(
        """
        UPDATE change_set cs SET n_files = sub.n
        FROM (
            SELECT csc.change_set_id, count(DISTINCT cf.file_id) AS n
            FROM change_set_commit csc
            JOIN commit_file cf ON cf.commit_id = csc.commit_id
            WHERE csc.change_set_id IN (SELECT id FROM change_set c2
                                         JOIN tmp_assign t ON t.key = c2.key)
            GROUP BY csc.change_set_id
        ) sub
        WHERE cs.id = sub.change_set_id
        """
    )
    conn.execute("DROP TABLE IF EXISTS tmp_assign")
    _collect_counts(conn, stats)
    return True


def _collect_counts(conn: psycopg.Connection, stats: CrossRepoStats) -> None:
    """Fill in the change-set counters from the current table state."""
    row = conn.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE signal = 'ticket'),
               count(*) FILTER (WHERE signal = 'temporal'),
               count(*) FILTER (WHERE pair_eligible)
        FROM change_set
        """
    ).fetchone()
    stats.change_sets, stats.ticket_sets, stats.temporal_sets, stats.eligible_sets = (
        int(row[0]), int(row[1]), int(row[2]), int(row[3])
    )


def _mark_eligibility(conn: psycopg.Connection, stats: CrossRepoStats) -> None:
    """Exclude change sets that sprawl across too many repositories."""
    cfg = get_config().crossrepo
    conn.execute(
        "UPDATE change_set SET pair_eligible = (n_repos <= %s AND n_repos >= 1)",
        (cfg.max_repos_per_changeset,),
    )
    stats.eligible_sets = int(
        conn.execute("SELECT count(*) FROM change_set WHERE pair_eligible").fetchone()[0]
    )


def _refresh_marginals(conn: psycopg.Connection) -> None:
    """Recompute the per-repo and per-file change-set marginals.

    These are the ``n_a``/``n_b`` of every cross-repo contingency table, and they
    count *change sets*, not commits -- so they are stored separately from
    ``file.pair_change_count`` rather than reusing it.
    """
    conn.execute("DELETE FROM repo_change_stats")
    conn.execute(
        """
        INSERT INTO repo_change_stats (repo_id, change_set_count, ticket_set_count,
                                       first_at, last_at)
        SELECT csc.repo_id,
               count(DISTINCT cs.id),
               count(DISTINCT cs.id) FILTER (WHERE cs.signal = 'ticket'),
               min(cs.first_at), max(cs.last_at)
        FROM change_set_commit csc
        JOIN change_set cs ON cs.id = csc.change_set_id
        WHERE cs.pair_eligible
        GROUP BY csc.repo_id
        """
    )
    conn.execute(
        """
        UPDATE file f SET xrepo_change_count = COALESCE(sub.n, 0)
        FROM (
            SELECT cf.file_id, count(DISTINCT cs.id) AS n
            FROM change_set cs
            JOIN change_set_commit csc ON csc.change_set_id = cs.id
            JOIN commit_file cf ON cf.commit_id = csc.commit_id
            WHERE cs.pair_eligible
            GROUP BY cf.file_id
        ) sub
        WHERE f.id = sub.file_id
        """
    )


def population(conn: psycopg.Connection) -> int:
    """``N`` for every cross-repo contingency table: eligible change sets."""
    row = conn.execute("SELECT count(*) FROM change_set WHERE pair_eligible").fetchone()
    return int(row[0]) if row else 0


def _build_repo_pairs(conn: psycopg.Connection) -> int:
    """Join change sets to themselves on repository, producing repo pairs."""
    cfg = get_config()
    half_life = max(cfg.analysis.recency_half_life_days, 1)
    min_support = max(cfg.crossrepo.min_support, 1)

    conn.execute("DELETE FROM repo_pair")
    row = conn.execute(
        """
        WITH cs_repo AS (
            SELECT DISTINCT csc.change_set_id, csc.repo_id
            FROM change_set_commit csc
            JOIN change_set cs ON cs.id = csc.change_set_id AND cs.pair_eligible
        )
        INSERT INTO repo_pair (repo_a_id, repo_b_id, n_ab, n_ab_ticket, w_ab,
                               first_co_change, last_co_change, distinct_authors)
        SELECT a.repo_id, b.repo_id,
               count(*),
               count(*) FILTER (WHERE cs.signal = 'ticket'),
               sum(power(0.5, EXTRACT(EPOCH FROM (now() - cs.last_at)) / 86400.0
                               / %(half_life)s)),
               min(cs.first_at), max(cs.last_at),
               count(DISTINCT cs.author_id)
        FROM cs_repo a
        JOIN cs_repo b ON b.change_set_id = a.change_set_id AND b.repo_id > a.repo_id
        JOIN change_set cs ON cs.id = a.change_set_id
        GROUP BY a.repo_id, b.repo_id
        HAVING count(*) >= %(min_support)s
        """,
        {"half_life": half_life, "min_support": min_support},
    ).rowcount
    return int(row or 0)


def _build_file_pairs(conn: psycopg.Connection) -> int:
    """Cross-repo file pairs, with a per-change-set-per-repo file cap.

    The cap is what keeps this tractable: without it the join is
    ``O(files_in_a * files_in_b)`` for every change set, and one release ticket
    touching hundreds of files in two repos would dominate the whole table.
    Files are ranked by overall churn so the cap keeps the most significant ones.
    """
    cfg = get_config()
    half_life = max(cfg.analysis.recency_half_life_days, 1)
    min_support = max(cfg.crossrepo.min_support, 1)
    file_cap = max(cfg.crossrepo.max_files_per_repo_per_changeset, 1)

    conn.execute("DELETE FROM xrepo_file_pair")
    row = conn.execute(
        """
        WITH cs_file AS (
            SELECT DISTINCT cs.id AS change_set_id, cs.signal, cs.last_at,
                   csc.repo_id, cf.file_id, f.change_count
            FROM change_set cs
            JOIN change_set_commit csc ON csc.change_set_id = cs.id
            JOIN commit_file cf ON cf.commit_id = csc.commit_id
            JOIN file f ON f.id = cf.file_id
            WHERE cs.pair_eligible AND cs.n_repos > 1
        ),
        ranked AS (
            SELECT *, row_number() OVER (
                       PARTITION BY change_set_id, repo_id
                       ORDER BY change_count DESC, file_id
                   ) AS rn
            FROM cs_file
        ),
        capped AS (
            SELECT change_set_id, signal, last_at, repo_id, file_id
            FROM ranked WHERE rn <= %(file_cap)s
        )
        INSERT INTO xrepo_file_pair (file_a_id, file_b_id, repo_a_id, repo_b_id,
                                     n_ab, n_ab_ticket, w_ab,
                                     first_co_change, last_co_change)
        SELECT
            LEAST(a.file_id, b.file_id),
            GREATEST(a.file_id, b.file_id),
            CASE WHEN a.file_id < b.file_id THEN a.repo_id ELSE b.repo_id END,
            CASE WHEN a.file_id < b.file_id THEN b.repo_id ELSE a.repo_id END,
            count(*),
            count(*) FILTER (WHERE a.signal = 'ticket'),
            sum(power(0.5, EXTRACT(EPOCH FROM (now() - a.last_at)) / 86400.0
                            / %(half_life)s)),
            min(a.last_at), max(a.last_at)
        FROM capped a
        JOIN capped b
          ON b.change_set_id = a.change_set_id
         AND b.repo_id <> a.repo_id
         AND b.file_id > a.file_id
        GROUP BY 1, 2, 3, 4
        HAVING count(*) >= %(min_support)s
        """,
        {"half_life": half_life, "min_support": min_support, "file_cap": file_cap},
    ).rowcount
    return int(row or 0)


def _score(conn: psycopg.Connection, level: str) -> int:
    """Materialise all 31 measures for one cross-repo level.

    Identical arithmetic to :mod:`git_synapse.analysis.score`; only the source of the
    counts differs. The measures themselves are reused untouched from the
    registry, which is the point of keeping them free of database concerns.
    """
    n_total = population(conn)

    if level == "repo":
        metric_table = "repo_pair_metric"
        key_cols = ("repo_a_id", "repo_b_id")
        source = """
            SELECT p.repo_a_id, p.repo_b_id, p.n_ab,
                   COALESCE(sa.change_set_count, 0), COALESCE(sb.change_set_count, 0)
            FROM repo_pair p
            LEFT JOIN repo_change_stats sa ON sa.repo_id = p.repo_a_id
            LEFT JOIN repo_change_stats sb ON sb.repo_id = p.repo_b_id
        """
        extra_cols: tuple[str, ...] = ()
        extra_select = ""
    elif level == "file":
        metric_table = "xrepo_file_pair_metric"
        key_cols = ("file_a_id", "file_b_id")
        source = """
            SELECT p.file_a_id, p.file_b_id, p.n_ab,
                   fa.xrepo_change_count, fb.xrepo_change_count,
                   p.repo_a_id, p.repo_b_id
            FROM xrepo_file_pair p
            JOIN file fa ON fa.id = p.file_a_id
            JOIN file fb ON fb.id = p.file_b_id
        """
        extra_cols = ("repo_a_id", "repo_b_id")
        extra_select = ""
    else:
        raise ValueError(f"unknown level {level!r}")

    conn.execute(f"DELETE FROM {metric_table}")
    if n_total <= 0:
        return 0

    columns = [*key_cols, *extra_cols, *_CELLS, *ALL_KEYS]
    written = 0

    with conn.cursor(name=f"xscore_{level}") as cur:
        cur.itersize = _SCORE_BATCH
        cur.execute(source)
        while True:
            rows = cur.fetchmany(_SCORE_BATCH)
            if not rows:
                break
            arr = np.array([r[:5] for r in rows], dtype=np.int64)
            table = Contingency.from_counts(
                n_ab=arr[:, 2], n_a=arr[:, 3], n_b=arr[:, 4], n_total=n_total
            )
            scores = [
                np.asarray(BY_KEY[k].compute(table), dtype=np.float64).tolist()
                for k in ALL_KEYS
            ]
            out = []
            for i, r in enumerate(rows):
                head = [int(r[0]), int(r[1])]
                if extra_cols:
                    head += [int(r[5]), int(r[6])]
                head += [int(r[2]), int(r[3]), int(r[4]), n_total]
                out.append(tuple(head) + tuple(s[i] for s in scores))
            written += copy_rows(metric_table, columns, out, conn=conn)

    return written
