"""Impact prediction: the ranked answer to "I am changing X, what else?".

Built entirely from what repositories **declare** about each other. A manifest
naming a dependency is dated, directional and provable; it needs no statistical
argument and cannot produce an edge between codebases that share no code.

This used to rank an ensemble of 29 measures over a time-binned, directed
co-change table. That table was measured and found unsound: two of the public
repositories in the test corpus, sharing no code at all, scored G2 = 570 against
each other, because two busy repositories occupy the same time bins whatever
they contain. Correlation over calendar time cannot tell propagation from a
shared release era, so it is gone.

What ranks an edge now
----------------------
Only facts, in order of weight:

* **declared** -- the consumer's manifest names the dependency at HEAD.
* **bump history** -- how many times the consumer has actually raised the
  version. A dependency bumped forty times is a live relationship; one declared
  and never moved is inert.
* **recency** -- when it was last bumped.
* **observed lag** -- the median delay between an upstream commit and the
  consumer picking it up, where a version resolved to one.

Every one of those is auditable back to a line in a file in a commit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import psycopg

from git_synapse.db.engine import connection, get_watermark, set_watermark

log = logging.getLogger(__name__)


@dataclass
class PredictStats:
    """Outcome of one impact rebuild."""

    sources: int = 0
    rows_written: int = 0
    declared_edges: int = 0
    bumped_edges: int = 0
    duration_s: float = 0.0


def _input_fingerprint(conn: psycopg.Connection) -> str:
    """Cheap signature of the inputs, so an unchanged graph is not rebuilt."""
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM repo_dependency),
               (SELECT count(*) FROM dep_bump),
               (SELECT COALESCE(max(bumped_at)::text, '') FROM dep_bump)
        """
    ).fetchone()
    return "|".join(str(v) for v in row)


def rebuild(conn: psycopg.Connection | None = None, force: bool = False) -> PredictStats:
    """Recompute ``repo_impact`` from the declared dependency graph.

    One row per declared ``(dependency -> consumer)`` edge. Skipped when neither
    ``repo_dependency`` nor ``dep_bump`` has changed since the last run.
    """

    def _run(c: psycopg.Connection) -> PredictStats:
        started = time.monotonic()
        stats = PredictStats()

        fingerprint = _input_fingerprint(c)
        if not force and get_watermark("predict_inputs") == fingerprint:
            log.debug("impact inputs unchanged; skipping rebuild")
            return stats

        c.execute("TRUNCATE repo_impact")
        c.execute(
            """
            WITH bumps AS (
                SELECT dep_repo_id, consumer_repo_id,
                       count(*)                       AS bump_count,
                       max(bumped_at)                 AS last_bump,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY adoption_seconds) / 86400.0 AS median_adoption_days
                  FROM dep_bump
                 WHERE dep_repo_id IS NOT NULL
              GROUP BY dep_repo_id, consumer_repo_id
            ),
            edges AS (
                SELECT d.dep_repo_id      AS source_repo_id,
                       d.consumer_repo_id AS target_repo_id,
                       TRUE               AS is_declared,
                       COALESCE(b.bump_count, 0) AS bump_count,
                       b.last_bump, b.median_adoption_days
                  FROM repo_dependency d
             LEFT JOIN bumps b ON b.dep_repo_id = d.dep_repo_id
                              AND b.consumer_repo_id = d.consumer_repo_id
                 WHERE d.dep_repo_id IS NOT NULL
                   AND d.dep_repo_id <> d.consumer_repo_id
              GROUP BY 1, 2, 3, 4, b.last_bump, b.median_adoption_days
                UNION
                -- A dependency dropped from the manifest but bumped in the past
                -- is still a real historical relationship.
                SELECT b.dep_repo_id, b.consumer_repo_id, FALSE,
                       b.bump_count, b.last_bump, b.median_adoption_days
                  FROM bumps b
                 WHERE b.dep_repo_id <> b.consumer_repo_id
                   AND NOT EXISTS (
                       SELECT 1 FROM repo_dependency d
                        WHERE d.dep_repo_id = b.dep_repo_id
                          AND d.consumer_repo_id = b.consumer_repo_id)
            ),
            scored AS (
                SELECT *,
                       -- Declared is the strong signal; bumps show the edge is
                       -- live; recency breaks ties. Bounded to [0, 1] so the
                       -- number is comparable across repositories.
                       LEAST(1.0,
                             (CASE WHEN is_declared THEN 0.5 ELSE 0.2 END)
                           + LEAST(0.3, bump_count * 0.02)
                           + CASE
                               WHEN last_bump IS NULL THEN 0.0
                               WHEN last_bump > now() - interval '90 days'  THEN 0.2
                               WHEN last_bump > now() - interval '365 days' THEN 0.1
                               ELSE 0.0
                             END
                       ) AS score
                  FROM edges
            )
            INSERT INTO repo_impact (
                source_repo_id, target_repo_id, score, rank_in_source,
                is_declared, has_bump_history, bump_count, median_adoption_days, features)
            SELECT source_repo_id, target_repo_id, score,
                   row_number() OVER (PARTITION BY source_repo_id ORDER BY score DESC),
                   is_declared, bump_count > 0, bump_count, median_adoption_days,
                   jsonb_build_object(
                       'scored_by', 'declared',
                       'bump_count', bump_count,
                       'last_bump', last_bump)
              FROM scored
            """
        )
        row = c.execute(
            """
            SELECT count(*), count(DISTINCT source_repo_id),
                   count(*) FILTER (WHERE is_declared),
                   count(*) FILTER (WHERE has_bump_history)
              FROM repo_impact
            """
        ).fetchone()
        stats.rows_written, stats.sources = int(row[0]), int(row[1])
        stats.declared_edges, stats.bumped_edges = int(row[2]), int(row[3])
        set_watermark("predict_inputs", fingerprint, c)
        stats.duration_s = time.monotonic() - started
        log.info("impact: %d edges over %d repositories in %.1fs",
                 stats.rows_written, stats.sources, stats.duration_s)
        return stats

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def impact_for(
    repo_id: int,
    limit: int = 20,
    declared_only: bool = False,
    min_score: float = 0.0,
) -> list[dict]:
    """What else to look at when changing ``repo_id``, ranked."""
    from git_synapse.db.engine import query

    clauses = ["i.source_repo_id = %(repo_id)s", "i.score >= %(min_score)s"]
    if declared_only:
        clauses.append("i.is_declared")
    return query(
        f"""
        SELECT i.*, r.name, r.full_name, r.primary_language, r.description,
               r.commit_count
        FROM repo_impact i
        JOIN repo r ON r.id = i.target_repo_id
        WHERE {' AND '.join(clauses)}
        ORDER BY (i.is_declared OR i.has_bump_history) DESC, i.score DESC
        LIMIT %(limit)s
        """,
        {"repo_id": repo_id, "limit": limit, "min_score": min_score},
    )


def upstream_of(repo_id: int, limit: int = 20) -> list[dict]:
    """Repositories whose changes tend to *precede* changes here.

    The reverse direction, and the one that answers the question behind the
    motivating case: the fix you are about to make in this repo may actually
    belong upstream.
    """
    from git_synapse.db.engine import query

    return query(
        """
        SELECT i.*, r.name, r.full_name, r.primary_language, r.description
        FROM repo_impact i
        JOIN repo r ON r.id = i.source_repo_id
        WHERE i.target_repo_id = %(repo_id)s
        ORDER BY (i.is_declared OR i.has_bump_history) DESC, i.score DESC
        LIMIT %(limit)s
        """,
        {"repo_id": repo_id, "limit": limit},
    )


def impact_chains(
    repo_id: int,
    max_depth: int = 3,
    min_score: float = 0.5,
    limit: int = 40,
) -> list[dict]:
    """Transitive impact paths over the prediction graph.

    Composes per-hop scores multiplicatively, so a weak hop can only weaken a
    path. Unlike :func:`git_synapse.analysis.query.repo_chains`, which walks raw
    conditional probabilities, this walks the ensemble score.

    Args:
        repo_id: repository to walk out from.
        max_depth: maximum hops; 2 gives A -> B -> C.
        min_score: per-hop floor.
        limit: maximum paths returned.

    Every hop carries evidence: ``rebuild`` writes an edge only from a declared
    dependency or an observed version bump, so there is no unvalidated tier to
    exclude. The statistical discovery path was removed after it scored AUC 0.63
    on which way the arrow points -- chaining through it produced paths like
    ``signer -> runtime -> teams``, where the second hop is activity confounding
    rather than coupling.
    """
    from git_synapse.db.engine import query

    return query(
        f"""
        WITH RECURSIVE walk AS (
            SELECT i.source_repo_id AS src, i.target_repo_id AS dst, 1 AS depth,
                   i.score AS path_score,
                   ARRAY[i.source_repo_id, i.target_repo_id] AS path,
                   ARRAY[round(i.score::numeric, 4)] AS hops,
                   ARRAY[i.is_declared] AS declared,
                   ARRAY[i.median_adoption_days] AS lags
            FROM repo_impact i
            WHERE i.source_repo_id = %(repo_id)s
              AND i.score >= %(min_score)s

            UNION ALL

            SELECT w.src, i.target_repo_id, w.depth + 1,
                   w.path_score * i.score,
                   w.path || i.target_repo_id,
                   w.hops || round(i.score::numeric, 4),
                   w.declared || i.is_declared,
                   w.lags || i.median_adoption_days
            FROM walk w
            JOIN repo_impact i ON i.source_repo_id = w.dst
            WHERE w.depth < %(depth)s
              AND i.score >= %(min_score)s
              AND NOT i.target_repo_id = ANY(w.path)
        )
        SELECT w.depth, w.path_score, w.path, w.hops, w.declared, w.lags,
               (SELECT array_agg(r.name ORDER BY ord)
                  FROM unnest(w.path) WITH ORDINALITY AS u(id, ord)
                  JOIN repo r ON r.id = u.id) AS repo_names
        FROM walk w
        WHERE w.depth >= 2
        ORDER BY w.path_score DESC
        LIMIT %(limit)s
        """,
        {"repo_id": repo_id, "depth": max(1, min(max_depth, 5)),
         "min_score": min_score, "limit": limit},
    )


def upstream_chains(
    repo_id: int, max_depth: int = 3, min_score: float = 0.5, limit: int = 25
) -> list[dict]:
    """Chains flowing *into* a repository: where a change here may originate.

    The inverse traversal of :func:`impact_chains`, and the one that answers the
    motivating question -- "I am editing runtime; the real fix may be two hops
    upstream in signer".
    """
    from git_synapse.db.engine import query

    return query(
        """
        WITH RECURSIVE walk AS (
            SELECT i.target_repo_id AS sink, i.source_repo_id AS cur, 1 AS depth,
                   i.score AS path_score,
                   ARRAY[i.target_repo_id, i.source_repo_id] AS path,
                   ARRAY[round(i.score::numeric, 4)] AS hops
            FROM repo_impact i
            WHERE i.target_repo_id = %(repo_id)s
              AND i.score >= %(min_score)s
              AND (i.is_declared OR i.has_bump_history)

            UNION ALL

            SELECT w.sink, i.source_repo_id, w.depth + 1,
                   w.path_score * i.score,
                   w.path || i.source_repo_id,
                   w.hops || round(i.score::numeric, 4)
            FROM walk w
            JOIN repo_impact i ON i.target_repo_id = w.cur
            WHERE w.depth < %(depth)s
              AND i.score >= %(min_score)s
              AND NOT i.source_repo_id = ANY(w.path)
              AND (i.is_declared OR i.has_bump_history)
        )
        SELECT w.depth, w.path_score, w.path, w.hops,
               (SELECT array_agg(r.name ORDER BY ord)
                  FROM unnest(w.path) WITH ORDINALITY AS u(id, ord)
                  JOIN repo r ON r.id = u.id) AS repo_names
        FROM walk w
        WHERE w.depth >= 2
        ORDER BY w.path_score DESC
        LIMIT %(limit)s
        """,
        {"repo_id": repo_id, "depth": max(1, min(max_depth, 5)),
         "min_score": min_score, "limit": limit},
    )
