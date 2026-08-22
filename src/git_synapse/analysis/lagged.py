"""Directed, time-lagged coupling: which repository's changes *precede* another's.

Why the existing model cannot answer this
-----------------------------------------
:mod:`git_synapse.analysis.aggregate` and :mod:`git_synapse.analysis.crossrepo` both build
*symmetric, simultaneous* tables: A and B are "coupled" if they appear in the
same commit or the same change set. That is structurally unable to express
propagation, which is the pattern that actually matters for a dependency chain::

    signer  merged 2026-06-26 22:11
    packager  bumped 2026-06-26 22:45   (+34 min)
    runtime  bumped 2026-06-27 00:36   (+2h25m)

A same-window symmetric measure sees three repos in one bucket and reports an
undirected triangle. It cannot say the arrow points *from* signer.

The construction
----------------
Discretise time into bins of ``bin_hours``. Each repository becomes a binary
vector over bins: did it change in that bin? For an ordered pair (A, B) and a
lag of ``k`` bins, form the 2x2 table over bins::

    a = #bins where A changed and B changed k bins later
    b = #bins where A changed and B did not change k bins later
    c = #bins where A did not change but B changed k bins later
    d = #bins where neither

That is a genuine contingency table, so **all 29 measures apply unchanged** --
they simply become directional, because swapping A and B, or negating k, gives a
different table. ``A -> B`` at lag 1 scoring far above ``B -> A`` at lag 1 is
exactly the statistical statement "A's changes precede B's".

Why this is fast
----------------
The whole computation is one matrix product per lag. With a binary matrix
``M`` of shape (repos, bins), the joint counts for every ordered pair at lag k
are ``M @ shift(M, k).T`` -- a single BLAS call producing an R x R matrix. For
272 repositories over six years of daily bins that is a 272 x 2200 matrix and a
handful of milliseconds, so sweeping several lags is essentially free.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import psycopg

from git_synapse.config import get_config
from git_synapse.db.engine import connection, copy_rows, get_watermark, set_watermark
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import ALL_KEYS, BY_KEY

log = logging.getLogger(__name__)

#: Lags, in bins, evaluated by default. 0 is "same bin" (simultaneous); the rest
#: probe increasing propagation delay. With the default 6-hour bins these cover
#: 0h, 6h, 12h, 1d, 2d, 3d, 1w and 2w.
#:
#: This set and the 6-hour bin width are not arbitrary. The observed propagation
#: lags in this corpus span two orders of magnitude -- `contracts -> telemetry` has a
#: median of 0.0 days while `gomi -> runtime` has 4.8 -- so a single fixed lag
#: systematically misses one regime. And coarser bins destroy the signal
#: outright: the motivating case propagated in 34 minutes and 2h25m, which a
#: 24-hour bin cannot resolve at all. Moving from 24h to 6h bins lifted
#: directional accuracy from 0.64 to 0.70.
DEFAULT_LAGS: tuple[int, ...] = (0, 1, 2, 4, 8, 12, 28, 56)

#: Commits dated before this are treated as clock errors, not history. Git
#: itself did not exist before 2005, so anything earlier is a corrupt author
#: date and must not be allowed to set the time origin.
PLAUSIBLE_EPOCH = "2005-01-01"


@dataclass
class LaggedStats:
    """Outcome of one lagged build."""

    bin_hours: int = 24
    n_bins: int = 0
    n_repos: int = 0
    lags: tuple[int, ...] = field(default_factory=tuple)
    rows_written: int = 0
    duration_s: float = 0.0


def _event_matrix(
    conn: psycopg.Connection, bin_hours: int
) -> tuple[np.ndarray, list[int], int]:
    """Build the binary (repos x bins) change-event matrix.

    A repository "changed" in a bin if any pair-eligible commit of that repo has
    a commit timestamp inside it. Merge commits and oversized commits are
    excluded for the same reason they are excluded from co-occurrence counting.

    Returns:
        ``(matrix, repo_ids, n_bins)`` with ``matrix`` in float32 so the joint
        counts come out of a single BLAS matrix product.
    """
    # Restrict to a plausible window before deriving the time origin. A handful
    # of commits in any large org carry a corrupt author date -- four in this
    # corpus sit at the Unix epoch -- and letting one of them set t0 stretched
    # the axis to 20,687 daily bins instead of ~2,200. That is not a cosmetic
    # problem: n_total is the N of every contingency table, so a tenfold
    # inflation of empty bins inflates the `d` cell and silently distorts every
    # measure that uses it.
    rows = conn.execute(
        """
        WITH valid AS (
            SELECT repo_id, committed_at
            FROM commit
            WHERE pair_eligible
              AND committed_at >= %(floor_date)s
              AND committed_at <= now() + interval '1 day'
        )
        SELECT v.repo_id,
               floor(
                   EXTRACT(EPOCH FROM (v.committed_at - bounds.t0))
                   / (%(bin_hours)s * 3600.0)
               )::bigint AS bin
        FROM valid v
        CROSS JOIN (SELECT min(committed_at) AS t0 FROM valid) bounds
        GROUP BY 1, 2
        """,
        {"bin_hours": bin_hours, "floor_date": PLAUSIBLE_EPOCH},
    ).fetchall()

    if not rows:
        return np.zeros((0, 0), dtype=np.float32), [], 0

    arr = np.array(rows, dtype=np.int64)
    repo_ids = sorted({int(r) for r in arr[:, 0]})
    index = {rid: i for i, rid in enumerate(repo_ids)}
    n_bins = int(arr[:, 1].max()) + 1

    matrix = np.zeros((len(repo_ids), n_bins), dtype=np.float32)
    for repo_id, bin_no in arr:
        matrix[index[int(repo_id)], int(bin_no)] = 1.0
    return matrix, repo_ids, n_bins


def _joint_at_lag(matrix: np.ndarray, lag: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Joint and marginal counts for every ordered pair at one lag.

    Args:
        matrix: (repos x bins) binary matrix.
        lag: bins to shift B forward relative to A.

    Returns:
        ``(joint, n_a, n_b, n_windows)`` where ``joint[i, j]`` counts bins in
        which repo i changed and repo j changed ``lag`` bins later.
    """
    n_bins = matrix.shape[1]
    if lag >= n_bins:
        n = matrix.shape[0]
        return np.zeros((n, n)), np.zeros(n), np.zeros(n), 0

    # Align the two views so index t in `a_view` and `b_view` refer to bin t and
    # bin t+lag respectively. Both are then restricted to the same number of
    # windows, which is what makes N identical for every cell of the table.
    a_view = matrix[:, : n_bins - lag] if lag else matrix
    b_view = matrix[:, lag:] if lag else matrix
    n_windows = a_view.shape[1]

    # One BLAS call gives the joint counts for all ordered pairs at once.
    joint = a_view @ b_view.T
    n_a = a_view.sum(axis=1)
    n_b = b_view.sum(axis=1)
    return joint, n_a, n_b, n_windows


def _input_fingerprint(conn: psycopg.Connection, bin_hours: int, lags: tuple[int, ...]) -> str:
    """Fingerprint of everything this stage depends on.

    The event matrix is derived purely from pair-eligible commit timestamps, so
    if no commit has been added or had its eligibility change, the matrix -- and
    therefore every output row -- is byte-identical. Note the fingerprint does
    NOT include the wall clock: this stage has no recency weighting, so time
    passing alone cannot change its result.
    """
    row = conn.execute(
        """
        SELECT count(*), COALESCE(max(id), 0), COALESCE(max(committed_at)::text, '')
        FROM commit WHERE pair_eligible
        """
    ).fetchone()
    return f"{row[0]}:{row[1]}:{row[2]}:{bin_hours}:{','.join(map(str, lags))}"


def rebuild(
    bin_hours: int | None = None,
    lags: tuple[int, ...] = DEFAULT_LAGS,
    min_support: int | None = None,
    conn: psycopg.Connection | None = None,
    force: bool = False,
) -> LaggedStats:
    """Compute and store directed lagged coupling for every ordered repo pair.

    A full rebuild rather than a delta, deliberately: the computation *is* one
    matrix product per lag, so there is no cheaper incremental form -- a delta
    would still have to multiply the changed repository's row against every
    other repository across every bin. Instead the whole stage is skipped when
    its inputs have not moved, which is the case on most nights.

    Args:
        bin_hours: width of a time bin. Smaller bins give sharper direction but
            fewer co-occurrences; 6h is the validated default.
        lags: lags in bins to evaluate.
        min_support: skip pairs whose joint count is below this.
        conn: reuse an open connection.
        force: recompute even if the input fingerprint is unchanged.
    """

    def _run(c: psycopg.Connection) -> LaggedStats:
        cfg = get_config()
        bins_h = bin_hours or cfg.crossrepo.lag_bin_hours
        support = min_support if min_support is not None else cfg.crossrepo.lag_min_support

        started = time.monotonic()

        fingerprint = _input_fingerprint(c, bins_h, tuple(lags))
        if not force and get_watermark("lagged") == fingerprint:
            existing = c.execute("SELECT count(*) FROM repo_lag_metric").fetchone()[0]
            log.info("lagged coupling: inputs unchanged; keeping %d rows", existing)
            return LaggedStats(bin_hours=bins_h, lags=tuple(lags),
                               rows_written=int(existing),
                               duration_s=time.monotonic() - started)

        matrix, repo_ids, n_bins = _event_matrix(c, bins_h)
        stats = LaggedStats(bin_hours=bins_h, n_bins=n_bins,
                            n_repos=len(repo_ids), lags=tuple(lags))
        if not len(repo_ids):
            return stats

        c.execute("TRUNCATE repo_lag_metric")
        columns = [
            "repo_a_id", "repo_b_id", "lag_bins", "bin_hours",
            "n_ab", "n_a", "n_b", "n_total", *ALL_KEYS,
        ]
        ids = np.array(repo_ids, dtype=np.int64)

        for lag in lags:
            joint, n_a, n_b, n_windows = _joint_at_lag(matrix, lag)
            if n_windows == 0:
                continue

            # Keep only ordered pairs with enough evidence. At lag 0 the table is
            # symmetric and the diagonal is meaningless, so self-pairs go.
            rows_i, rows_j = np.nonzero(joint >= max(support, 1))
            keep = rows_i != rows_j
            rows_i, rows_j = rows_i[keep], rows_j[keep]
            if not len(rows_i):
                continue

            table = Contingency.from_counts(
                n_ab=joint[rows_i, rows_j],
                n_a=n_a[rows_i],
                n_b=n_b[rows_j],
                n_total=n_windows,
            )
            scores = [
                np.asarray(BY_KEY[k].compute(table), dtype=np.float64) for k in ALL_KEYS
            ]

            payload = [
                (
                    int(ids[i]), int(ids[j]), int(lag), int(bins_h),
                    int(joint[i, j]), int(n_a[i]), int(n_b[j]), int(n_windows),
                    *(float(s[n]) for s in scores),
                )
                for n, (i, j) in enumerate(zip(rows_i, rows_j))
            ]
            stats.rows_written += copy_rows("repo_lag_metric", columns, payload, conn=c)
            log.debug("lag %d: %d ordered pairs", lag, len(payload))

        set_watermark("lagged", fingerprint, conn=c)
        stats.duration_s = time.monotonic() - started
        log.info(
            "lagged coupling: %d repos x %d bins of %dh, lags %s -> %d rows in %.1fs",
            stats.n_repos, stats.n_bins, bins_h, list(lags),
            stats.rows_written, stats.duration_s,
        )
        return stats

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def asymmetry(
    repo_a_id: int, repo_b_id: int, lag: int = 1, measure: str = "npmi"
) -> dict | None:
    """Compare ``A -> B`` against ``B -> A`` at one lag.

    The ratio of the two is the directional evidence: a value well above 1 means
    A's changes systematically precede B's rather than the reverse.
    """
    from git_synapse.db.engine import query_one
    from git_synapse.stats.registry import resolve

    key = resolve(measure).key
    row = query_one(
        f"""
        SELECT
          (SELECT {key} FROM repo_lag_metric
            WHERE repo_a_id=%(a)s AND repo_b_id=%(b)s AND lag_bins=%(lag)s) AS forward,
          (SELECT {key} FROM repo_lag_metric
            WHERE repo_a_id=%(b)s AND repo_b_id=%(a)s AND lag_bins=%(lag)s) AS reverse,
          (SELECT n_ab FROM repo_lag_metric
            WHERE repo_a_id=%(a)s AND repo_b_id=%(b)s AND lag_bins=%(lag)s) AS n_forward,
          (SELECT n_ab FROM repo_lag_metric
            WHERE repo_a_id=%(b)s AND repo_b_id=%(a)s AND lag_bins=%(lag)s) AS n_reverse
        """,
        {"a": repo_a_id, "b": repo_b_id, "lag": lag},
    )
    if row is None:
        return None
    fwd, rev = row.get("forward"), row.get("reverse")
    row["measure"] = key
    row["lag_bins"] = lag
    row["ratio"] = (fwd / rev) if (fwd and rev and rev != 0) else None
    return row
