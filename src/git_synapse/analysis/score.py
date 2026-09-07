"""Materialisation of the 31 association measures over the pair tables.

This is a pure function of the aggregates: it reads ``(n_ab, n_a, n_b, N)`` for
each pair, evaluates every measure in the registry, and writes the results to
``file_pair_metric`` / ``dir_pair_metric``. Nothing here reads git or the atomic
tables, so re-scoring after adding a measure costs one pass over the pair table
and no re-ingest.

Measures are evaluated in vectorised numpy batches rather than row by row.
Scoring is therefore dominated by the database round trip rather than the
arithmetic: 31 measures over a 200k-row batch is a few hundred milliseconds.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import psycopg

from git_synapse.config import get_config
from git_synapse.db.engine import connection, copy_rows
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import ALL_KEYS, BY_KEY

log = logging.getLogger(__name__)

#: Column order for the metric tables: the four contingency cells, then every
#: measure in registry order. Shared by the file and directory variants.
_CELL_COLUMNS = ("n_ab", "n_a", "n_b", "n_total")


@dataclass
class ScoreStats:
    """Outcome of one scoring pass."""

    repo_id: int
    file_pairs: int = 0
    dir_pairs: int = 0
    duration_s: float = 0.0


def score_repo(repo_id: int, conn: psycopg.Connection | None = None) -> ScoreStats:
    """Recompute and persist every measure for one repository's pairs."""

    def _run(c: psycopg.Connection) -> ScoreStats:
        started = time.monotonic()
        stats = ScoreStats(repo_id=repo_id)
        stats.file_pairs = _score_level(c, repo_id, level="file")
        stats.dir_pairs = _score_level(c, repo_id, level="dir")
        stats.duration_s = time.monotonic() - started
        log.info(
            "repo %s scored in %.1fs: %d file pairs, %d dir pairs",
            repo_id, stats.duration_s, stats.file_pairs, stats.dir_pairs,
        )
        return stats

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def _level_sql(level: str) -> tuple[str, str, str, str, str]:
    """Return the table and column names for a granularity level.

    Returns:
        ``(pair_table, metric_table, entity_table, col_a, col_b)``
    """
    if level == "file":
        return "file_pair", "file_pair_metric", "file", "file_a_id", "file_b_id"
    if level == "dir":
        return "dir_pair", "dir_pair_metric", "directory", "dir_a_id", "dir_b_id"
    raise ValueError(f"unknown level {level!r}; expected 'file' or 'dir'")


def _score_level(conn: psycopg.Connection, repo_id: int, level: str) -> int:
    """Score every pair at one granularity level, batching through numpy."""
    _pair_table, metric_table, _entity_table, col_a, col_b = _level_sql(level)

    population = conn.execute(
        "SELECT pair_population FROM repo WHERE id = %s", (repo_id,)
    ).fetchone()
    n_total = int(population[0]) if population and population[0] else 0
    if n_total <= 0:
        log.debug("repo %s has no pair-eligible commits; nothing to score", repo_id)
        conn.execute(f"DELETE FROM {metric_table} WHERE repo_id = %s", (repo_id,))
        return 0

    conn.execute(f"DELETE FROM {metric_table} WHERE repo_id = %s", (repo_id,))

    columns = ["repo_id", col_a, col_b, *_CELL_COLUMNS, *ALL_KEYS]
    written = 0
    for batch in _iter_pair_batches(conn, repo_id, level, n_total):
        rows = _score_batch(repo_id, batch)
        written += copy_rows(metric_table, columns, rows, conn=conn)

    return written


@dataclass
class _Batch:
    """One chunk of pairs with their marginals, ready for vectorised scoring."""

    a_ids: np.ndarray
    b_ids: np.ndarray
    n_ab: np.ndarray
    n_a: np.ndarray
    n_b: np.ndarray
    n_total: int


def _iter_pair_batches(
    conn: psycopg.Connection, repo_id: int, level: str, n_total: int
) -> Iterator[_Batch]:
    """Stream pairs joined to their marginals, in fixed-size batches.

    A named (server-side) cursor is used so a repository with tens of millions
    of pairs never materialises its full result set in the client.
    """
    pair_table, _, entity_table, col_a, col_b = _level_sql(level)
    batch_size = max(get_config().analysis.score_batch_size, 1000)

    sql = f"""
        SELECT p.{col_a}, p.{col_b}, p.n_ab, ea.pair_change_count, eb.pair_change_count
        FROM {pair_table} p
        JOIN {entity_table} ea ON ea.id = p.{col_a}
        JOIN {entity_table} eb ON eb.id = p.{col_b}
        WHERE p.repo_id = %s
    """

    with conn.cursor(name=f"score_{level}_{repo_id}") as cur:
        cur.itersize = batch_size
        cur.execute(sql, (repo_id,))
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            arr = np.array(rows, dtype=np.int64)
            yield _Batch(
                a_ids=arr[:, 0],
                b_ids=arr[:, 1],
                n_ab=arr[:, 2],
                n_a=arr[:, 3],
                n_b=arr[:, 4],
                n_total=n_total,
            )


def _score_batch(repo_id: int, batch: _Batch) -> list[tuple]:
    """Evaluate every registered measure over a batch and build COPY rows.

    The cells written are the ones the measures were computed from, not the raw
    aggregates. ``Contingency.from_counts`` clamps input to the feasible region
    -- ``n_ab`` above either marginal comes down, a marginal above ``N`` comes
    down, and inclusion-exclusion can force ``a`` *up* from a reported zero when
    ``n_a + n_b > N``. Writing the raw numbers beside scores derived from the
    clamped ones breaks the property the whole design rests on: that any number
    in the UI traces back to four counts, and those four counts reproduce it.

    Clamping should never fire on data this pipeline produced -- the marginals
    come from the same commits as the joint count -- so a difference means an
    aggregate is stale, and is logged rather than passed over.
    """
    table = Contingency.from_counts(
        n_ab=batch.n_ab, n_a=batch.n_a, n_b=batch.n_b, n_total=batch.n_total
    )
    scores = [BY_KEY[key].compute(table) for key in ALL_KEYS]

    cell_ab = np.asarray(table.a, dtype=np.int64)
    cell_a = np.asarray(table.a + table.b, dtype=np.int64)
    cell_b = np.asarray(table.a + table.c, dtype=np.int64)
    adjusted = int(np.count_nonzero(
        (cell_ab != np.asarray(batch.n_ab))
        | (cell_a != np.asarray(batch.n_a))
        | (cell_b != np.asarray(batch.n_b))))
    if adjusted:
        log.warning(
            "repo %s: %d pair(s) had infeasible counts and were clamped to the "
            "feasible table before scoring; an aggregate is likely stale",
            repo_id, adjusted)

    # Transpose column-wise arrays into row tuples for COPY. zip over the
    # arrays is materially faster than indexing each array per row.
    return [
        (repo_id, int(a), int(b), int(ab), int(na), int(nb), batch.n_total, *values)
        # strict: zip stops at the shortest input, so a measure returning
        # fewer values than there are pairs would silently drop rows from the
        # COPY -- pairs missing from the metric table, with nothing raised.
        for a, b, ab, na, nb, values in zip(
            batch.a_ids,
            batch.b_ids,
            cell_ab,
            cell_a,
            cell_b,
            zip(*(np.asarray(s, dtype=np.float64).tolist() for s in scores),
                strict=True),
            strict=True,
        )
    ]


def score_all(conn: psycopg.Connection | None = None) -> list[ScoreStats]:
    """Score every enabled repository. Used by the full-rebuild command."""

    def _run(c: psycopg.Connection) -> list[ScoreStats]:
        rows = c.execute("SELECT id FROM repo WHERE is_enabled ORDER BY id").fetchall()
        return [score_repo(int(r[0]), c) for r in rows]

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)
