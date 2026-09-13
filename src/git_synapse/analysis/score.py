"""Materialisation of the 31 association measures over the pair tables."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import aliased

from git_synapse.config import get_config
from git_synapse.db.orm import models, session_scope
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import ALL_KEYS, BY_KEY

log = logging.getLogger(__name__)

_CELL_COLUMNS = ("n_ab", "n_a", "n_b", "n_total")


@dataclass
class ScoreStats:
    """Outcome of one scoring pass."""

    repo_id: int
    file_pairs: int = 0
    dir_pairs: int = 0
    duration_s: float = 0.0


def score_repo(repo_id: int, conn: object | None = None) -> ScoreStats:
    """Recompute and persist every measure for one repository's pairs."""

    def _run(c: object) -> ScoreStats:
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
    with session_scope() as own:
        return _run(own)


def _level_sql(level: str) -> tuple[str, str, str, str, str]:
    """Return the table and column names for a granularity level."""
    if level == "file":
        return "file_pair", "file_pair_metric", "file", "file_a_id", "file_b_id"
    if level == "dir":
        return "dir_pair", "dir_pair_metric", "directory", "dir_a_id", "dir_b_id"
    raise ValueError(f"unknown level {level!r}; expected 'file' or 'dir'")


def _score_level(conn: object, repo_id: int, level: str) -> int:
    """Score every pair at one granularity level, batching through numpy."""
    _pair_table, _metric_table, _entity_table, col_a, col_b = _level_sql(level)

    Repo = models().Repo
    population = conn.get(Repo, repo_id)
    n_total = int(population.pair_population or 0) if population else 0
    if n_total <= 0:
        log.debug("repo %s has no pair-eligible commits; nothing to score", repo_id)
        conn.query(getattr(models(), "FilePairMetric" if level == "file" else "DirPairMetric")).filter_by(
            repo_id=repo_id
        ).delete(synchronize_session=False)
        return 0

    Metric = getattr(models(), "FilePairMetric" if level == "file" else "DirPairMetric")
    conn.query(Metric).filter_by(repo_id=repo_id).delete(synchronize_session=False)

    columns = ["repo_id", col_a, col_b, *_CELL_COLUMNS, *ALL_KEYS]
    written = 0
    for batch in _iter_pair_batches(conn, repo_id, level, n_total):
        rows = _score_batch(repo_id, batch)
        mappings = [dict(zip(columns, row, strict=True)) for row in rows]
        if mappings:
            conn.bulk_insert_mappings(Metric, mappings)
        written += len(mappings)

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
    conn: object, repo_id: int, level: str, n_total: int
) -> Iterator[_Batch]:
    """Stream pairs joined to their marginals, in fixed-size batches."""
    _pair_table, _, _entity_table, col_a, col_b = _level_sql(level)
    batch_size = max(get_config().analysis.score_batch_size, 1000)
    Pair = getattr(models(), "FilePair" if level == "file" else "DirPair")
    Entity = getattr(models(), "File" if level == "file" else "Directory")
    EntityA, EntityB = aliased(Entity), aliased(Entity)
    rows = conn.query(
        getattr(Pair, col_a), getattr(Pair, col_b), Pair.n_ab,
        EntityA.pair_change_count.label("n_a"), EntityB.pair_change_count.label("n_b")
    ).join(EntityA, EntityA.id == getattr(Pair, col_a)).join(
        EntityB, EntityB.id == getattr(Pair, col_b)
    ).filter(Pair.repo_id == repo_id).yield_per(batch_size)
    chunk: list[tuple] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) < batch_size:
            continue
        arr = np.array(chunk, dtype=np.int64)
        yield _Batch(a_ids=arr[:, 0], b_ids=arr[:, 1], n_ab=arr[:, 2],
                     n_a=arr[:, 3], n_b=arr[:, 4], n_total=n_total)
        chunk = []
    if chunk:
        arr = np.array(chunk, dtype=np.int64)
        yield _Batch(a_ids=arr[:, 0], b_ids=arr[:, 1], n_ab=arr[:, 2],
                     n_a=arr[:, 3], n_b=arr[:, 4], n_total=n_total)


def _score_batch(repo_id: int, batch: _Batch) -> list[tuple]:
    """Evaluate every registered measure over a batch and build ORM rows."""
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

    return [
        (repo_id, int(a), int(b), int(ab), int(na), int(nb), batch.n_total, *values)
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


def score_all(conn: object | None = None) -> list[ScoreStats]:
    """Score every enabled repository. Used by the full-rebuild command."""

    def _run(c: object) -> list[ScoreStats]:
        Repo = models().Repo
        rows = c.query(Repo).filter_by(is_enabled=True).order_by(Repo.id).all()
        return [score_repo(int(repo.id), c) for repo in rows]

    if conn is not None:
        return _run(conn)
    with session_scope() as own:
        return _run(own)
