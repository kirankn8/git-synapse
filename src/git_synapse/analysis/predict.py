"""Impact prediction: the ranked answer to "I am changing X, what else?".

This module is where the measured findings become a product surface. Three
results from :mod:`git_synapse.analysis.validate`, all on this corpus, drive its
design:

1. **Statistics alone cannot tell direction.** Ranking all ~74,000 ordered
   repository pairs by a single lagged measure reaches AUC 0.80 at best (lag 0,
   where a symmetric measure is 0.50 directional by construction; the best
   directional accuracy anywhere is 0.63 at lag 4, where AUC is 0.74)
   (``russell_rao`` at lag 0) -- but its directional accuracy is only ~0.63.
   That combination is the tell: ``russell_rao`` is ``a / N``, pure joint
   frequency, so it scores well by ranking *both repos are busy* and is close to
   a coin flip on which way the arrow points. A high AUC here is not the same as
   a useful answer.

2. **Structure is a decisive prior.** Restricting candidates to *declared*
   dependencies raises the base rate from 0.23% to 82% -- a ~350x lift -- before a
   single measure is evaluated. But structure alone is not enough either: of
   telemetry's 9 declared internal dependencies, 1 has never once co-changed.

3. **Together they reach AUC 0.86 in sample** (measured 0.859 over the shipped
   `repo_impact.score`, restricted to declared candidates). Held out in time --
   features from before 2025-01-01, labels from after -- it is 0.69. Treat 0.86
   as the optimistic bound and 0.69 as the honest one. No cross-validation
   figure is quoted: the ensemble has no fitted parameters, so folds train
   nothing and their spread is subsample noise. That is the configuration this
   module implements.

The ensemble
------------
An **unweighted rank-average** of the lagged measures, each taken at its best
lag per pair. Deliberately unweighted: with only 133 labelled candidate edges,
fitting weights would overfit, and the unweighted average already scores within
noise of the best label-selected combination (0.9289 vs 0.9320, where the latter
is inflated by selecting features on the evaluation labels).

Taking the best lag per pair matters because propagation delay varies by an
order of magnitude across the org -- ``contracts -> telemetry`` has a median lag of
0.0 days while ``gomi -> runtime`` has 4.8 days -- so a single fixed lag
systematically misses one regime or the other.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import numpy as np
import psycopg

from git_synapse.config import get_config
from git_synapse.db.engine import connection, copy_rows, get_watermark, set_watermark
from git_synapse.stats.registry import ALL_KEYS

log = logging.getLogger(__name__)

#: Measures entering the ensemble. Chosen for construction diversity rather than
#: measured performance: one directional conditional, one significance test, one
#: frequency term, two similarity coefficients and one normalised information
#: measure. Selecting on measured AUC would leak the evaluation labels.
ENSEMBLE_MEASURES: tuple[str, ...] = (
    "confidence_ab",          # directional: P(target changes | source changed)
    "log_likelihood_ratio",   # significance, well-behaved on rare events
    "russell_rao",            # raw joint frequency
    "ochiai",                 # similarity, robust to unbalanced marginals
    "t_score",                # frequency-weighted confidence
    "npmi",                   # bounded information-theoretic association
)

#: Measures used for DISCOVERY of undeclared coupling, deliberately different
#: from ENSEMBLE_MEASURES above.
#:
#: The ensemble was validated *within* the declared candidate set, where the base
#: rate is 82%. Applying it to all ~26,000 ordered pairs reintroduces the
#: activity confounding: measures with a raw frequency term (russell_rao,
#: t_score) rank a repository that commits every day as coupled to everything.
#: On this corpus that put `teams`, `nickfury` and `mural` above `signer` for
#: runtime, which is simply wrong -- and note russell_rao still scores the highest
#: GLOBAL AUC (0.80) while managing only 0.63 directional accuracy, so ranking
#: quality on a label set is no substitute for getting the direction right.
#:
#: Discovery therefore uses only measures normalised by both marginals, so a
#: high base rate cannot manufacture a score. NPMI divides by the joint
#: self-information; phi is a correlation coefficient; both are bounded and
#: signed.
DISCOVERY_MEASURES: tuple[str, ...] = ("npmi", "phi", "ochiai")

#: Percentile floor for an undeclared pair to be surfaced at all.
UNDECLARED_FLOOR = 0.97

#: Minimum co-occurring bins before an undeclared pair is trusted. Without this
#: the discovery measures happily award a perfect NPMI to a pair seen twice.
UNDECLARED_MIN_SUPPORT = 12

#: Hard cap on undeclared suggestions per source repository, so a hub repo that
#: correlates with everything cannot flood its own shortlist.
MAX_UNDECLARED_PER_SOURCE = 6


@dataclass
class PredictStats:
    """Outcome of one impact rebuild."""

    sources: int = 0
    rows_written: int = 0
    declared_edges: int = 0
    undeclared_surfaced: int = 0
    bin_hours: int = 0
    duration_s: float = 0.0


def _rank_normalise(values: np.ndarray) -> np.ndarray:
    """Map values to [0, 1] by rank, so incomparable scales can be averaged.

    Rank-normalising rather than z-scoring is deliberate: several measures are
    heavy-tailed (chi-square, log-likelihood, association strength are unbounded
    above), and a single outlier pair would otherwise dominate the mean.
    """
    n = len(values)
    if n <= 1:
        return np.zeros(n)
    order = np.argsort(np.argsort(values, kind="mergesort"), kind="mergesort")
    return order / (n - 1)


def _input_fingerprint(conn: psycopg.Connection) -> str:
    """Fingerprint of the three tables impact is derived from."""
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM repo_lag_metric),
               (SELECT COALESCE(max(computed_at)::text,'') FROM repo_lag_metric),
               (SELECT count(*) FROM repo_dependency),
               (SELECT count(*) FROM dep_bump)
        """
    ).fetchone()
    return ":".join(str(x) for x in row)


def rebuild(
    conn: psycopg.Connection | None = None, force: bool = False
) -> PredictStats:
    """Recompute ``repo_impact`` for every repository.

    Requires ``repo_lag_metric`` (from :mod:`git_synapse.analysis.lagged`),
    ``repo_dependency`` and ``dep_bump`` (from :mod:`git_synapse.analysis.depbump`).
    Skipped when none of those three has changed.
    """

    def _run(c: psycopg.Connection) -> PredictStats:
        started = time.monotonic()
        stats = PredictStats()

        fingerprint = _input_fingerprint(c)
        if not force and get_watermark("impact") == fingerprint:
            existing = c.execute("SELECT count(*) FROM repo_impact").fetchone()[0]
            log.info("impact: inputs unchanged; keeping %d edges", existing)
            stats.rows_written = int(existing)
            stats.duration_s = time.monotonic() - started
            return stats

        all_keys = tuple(dict.fromkeys(ENSEMBLE_MEASURES + DISCOVERY_MEASURES))
        cols = ", ".join(all_keys)
        rows = c.execute(
            f"""
            SELECT repo_a_id, repo_b_id, lag_bins, bin_hours, n_ab, {cols}
            FROM repo_lag_metric
            """
        ).fetchall()
        if not rows:
            log.warning("no lagged metrics; run `git-synapse lagged` first")
            return stats

        n_measures = len(all_keys)
        # Best value per (pair, measure) across all lags, plus the lag at which
        # the directional confidence peaked -- that is the propagation delay the
        # data actually supports for this pair.
        best: dict[tuple[int, int], list[float]] = {}
        best_lag: dict[tuple[int, int], int] = {}
        support: dict[tuple[int, int], int] = {}
        bin_hours = int(rows[0][3])

        for r in rows:
            pair = (int(r[0]), int(r[1]))
            slot = best.get(pair)
            if slot is None:
                slot = best[pair] = [0.0] * n_measures
            for i in range(n_measures):
                value = r[5 + i]
                if value is not None and float(value) > slot[i]:
                    slot[i] = float(value)
                    if i == 0:  # confidence_ab defines the characteristic lag
                        best_lag[pair] = int(r[2])
            support[pair] = max(support.get(pair, 0), int(r[4] or 0))

        pairs = list(best.keys())
        matrix = np.array([best[p] for p in pairs], dtype=np.float64)
        matrix = np.where(np.isfinite(matrix), matrix, 0.0)

        # Rank-normalise every measure once, then build two ensembles from the
        # same columns: the validated one for structural candidates, and a
        # confounder-resistant one for discovery.
        col = {key: _rank_normalise(matrix[:, i]) for i, key in enumerate(all_keys)}
        ensemble = np.mean([col[k] for k in ENSEMBLE_MEASURES], axis=0)
        discovery = np.mean([col[k] for k in DISCOVERY_MEASURES], axis=0)

        declared = {
            (int(r[0]), int(r[1]))
            for r in c.execute(
                "SELECT dep_repo_id, consumer_repo_id FROM repo_dependency"
                " WHERE dep_repo_id IS NOT NULL"
            ).fetchall()
        }
        bumps = {
            (int(r[0]), int(r[1])): (int(r[2]), float(r[3]) if r[3] is not None else None)
            for r in c.execute(
                """
                SELECT dep_repo_id, consumer_repo_id, count(*),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_seconds) / 86400.0
                FROM dep_bump
                WHERE dep_repo_id IS NOT NULL AND dep_repo_id <> consumer_repo_id
                GROUP BY 1, 2
                """
            ).fetchall()
        }

        # Declared and bump-backed edges are always kept: they carry structural
        # or ground-truth evidence regardless of score. Undeclared candidates are
        # kept only in the top percentile AND only the strongest few per source,
        # so a hub repository cannot flood its own shortlist.
        keep: list[int] = []
        undeclared_by_source: dict[int, list[tuple[float, int]]] = {}
        for i, pair in enumerate(pairs):
            if pair in declared or pair in bumps:
                keep.append(i)
            elif (
                discovery[i] >= UNDECLARED_FLOOR
                and support.get(pair, 0) >= UNDECLARED_MIN_SUPPORT
            ):
                undeclared_by_source.setdefault(pair[0], []).append((discovery[i], i))

        for candidates in undeclared_by_source.values():
            candidates.sort(reverse=True)
            keep.extend(i for _, i in candidates[:MAX_UNDECLARED_PER_SOURCE])

        # Rank within each source repository: the product question is always
        # "given I am changing THIS, what else?", never a global ordering.
        by_source: dict[int, list[int]] = {}
        for i in keep:
            by_source.setdefault(pairs[i][0], []).append(i)

        payload = []
        for source, indices in by_source.items():
            # Structural evidence outranks statistical discovery, then score.
            indices.sort(
                key=lambda i: (
                    0 if (pairs[i] in declared or pairs[i] in bumps) else 1,
                    -(ensemble[i] if (pairs[i] in declared or pairs[i] in bumps)
                      else discovery[i]),
                )
            )
            for rank, i in enumerate(indices, start=1):
                pair = pairs[i]
                bump_count, median_lag = bumps.get(pair, (0, None))
                is_declared = pair in declared
                if is_declared:
                    stats.declared_edges += 1
                elif bump_count == 0:
                    stats.undeclared_surfaced += 1
                # A declared or bump-backed edge is scored by the validated
                # ensemble; a discovered one by the confounder-resistant score.
                # Mixing them in one column would misrepresent confidence.
                scored_by_ensemble = is_declared or bump_count > 0
                payload.append(
                    (
                        pair[0],
                        pair[1],
                        float(ensemble[i] if scored_by_ensemble else discovery[i]),
                        rank,
                        is_declared,
                        bump_count > 0,
                        bump_count,
                        median_lag,
                        best_lag.get(pair),
                        bin_hours,
                        json.dumps(
                            {
                                **{
                                    key: round(float(matrix[i, j]), 6)
                                    for j, key in enumerate(all_keys)
                                },
                                "support_bins": support.get(pair, 0),
                                "ensemble": round(float(ensemble[i]), 6),
                                "discovery": round(float(discovery[i]), 6),
                                "scored_by": "ensemble" if scored_by_ensemble else "discovery",
                            }
                        ),
                    )
                )

        c.execute("TRUNCATE repo_impact")
        stats.rows_written = copy_rows(
            "repo_impact",
            [
                "source_repo_id", "target_repo_id", "score", "rank_in_source",
                "is_declared", "has_bump_history", "bump_count", "median_lag_days",
                "best_lag_bins", "bin_hours", "features",
            ],
            payload,
            conn=c,
        )
        stats.sources = len(by_source)
        stats.bin_hours = bin_hours
        set_watermark("impact", fingerprint)
        stats.duration_s = time.monotonic() - started

        log.info(
            "impact: %d rows across %d source repos (%d declared, %d undeclared "
            "above %.2f) in %.1fs",
            stats.rows_written, stats.sources, stats.declared_edges,
            stats.undeclared_surfaced, UNDECLARED_FLOOR, stats.duration_s,
        )
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
    validated_only: bool = True,
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
        validated_only: traverse only declared or bump-backed hops. **On by
            default and rarely worth turning off**: discovery edges are scored on
            a different, unvalidated scale, and chaining through them produced
            paths like ``signer -> runtime -> teams`` where the second hop is
            activity confounding rather than coupling.
    """
    from git_synapse.db.engine import query

    hop_filter = "AND (i.is_declared OR i.has_bump_history)" if validated_only else ""
    return query(
        f"""
        WITH RECURSIVE walk AS (
            SELECT i.source_repo_id AS src, i.target_repo_id AS dst, 1 AS depth,
                   i.score AS path_score,
                   ARRAY[i.source_repo_id, i.target_repo_id] AS path,
                   ARRAY[round(i.score::numeric, 4)] AS hops,
                   ARRAY[i.is_declared] AS declared,
                   ARRAY[i.median_lag_days] AS lags
            FROM repo_impact i
            WHERE i.source_repo_id = %(repo_id)s
              AND i.score >= %(min_score)s
              {hop_filter}

            UNION ALL

            SELECT w.src, i.target_repo_id, w.depth + 1,
                   w.path_score * i.score,
                   w.path || i.target_repo_id,
                   w.hops || round(i.score::numeric, 4),
                   w.declared || i.is_declared,
                   w.lags || i.median_lag_days
            FROM walk w
            JOIN repo_impact i ON i.source_repo_id = w.dst
            WHERE w.depth < %(depth)s
              AND i.score >= %(min_score)s
              AND NOT i.target_repo_id = ANY(w.path)
              {hop_filter}
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
