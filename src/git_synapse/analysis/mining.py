"""Mining layer: architectural and risk analyses derived from the same atoms.

Three analyses that answer questions the coupling tables alone do not:

**De-facto modules.** Label propagation over the file-coupling graph finds the
groups of files that actually behave as one unit. The valuable output is not the
clustering but its *disagreement* with the directory tree: a cluster spanning
several top-level folders is a module the codebase has grown without declaring.

**Coupling drift.** The same association recomputed on a recent window and a
historical one. A pair strongly coupled three years ago but not since is a
finished refactor, not live design coupling -- and reporting it as current is one
of the easier ways to mislead an agent.

**Ownership risk.** Herfindahl-Hirschman concentration over each author's share
of a file's commits, which yields an *effective* contributor count. Three authors
splitting commits 98/1/1 has a bus factor near 1, not 3, and only a
concentration measure sees that. Combined with churn and coupling into a
composite that ranks the files where a single departure would hurt most.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
import psycopg

from git_synapse.db.engine import connection, copy_rows, query

log = logging.getLogger(__name__)

#: Iterations of label propagation. Converges quickly on sparse graphs; ten is
#: comfortably past the point where labels stop moving on this corpus.
LABEL_PROPAGATION_ROUNDS = 10

#: Minimum co-change support for an edge to participate in clustering. Weak
#: edges are exactly the ones that merge unrelated clusters into one blob.
CLUSTER_MIN_SUPPORT = 3

#: Recent window for drift, in days. The preceding history is the comparison.
DRIFT_WINDOW_DAYS = 365

#: Shrinkage constant for the risk score's evidence weight.
#:
#: Percentiles are computed within a repository so that a 300-commit utility and
#: a 14,000-commit monolith are comparable. The side effect is that EVERY repo's
#: busiest file scores churn_pct = 1.0, so a README with five edits in a
#: throwaway repo tied with a genuine hotspot -- dozens of files pinned at
#: exactly 2.000. Weighting by ``n / (n + k)`` shrinks low-evidence files toward
#: zero while leaving well-observed ones essentially untouched: at k = 25, five
#: changes carries 17% weight, fifty carries 67%, and five hundred carries 95%.
RISK_EVIDENCE_K = 25


@dataclass
class MiningStats:
    """Row counts from one mining pass."""

    clusters: int = 0
    clustered_files: int = 0
    cross_directory_clusters: int = 0
    drift_rows: int = 0
    emerging: int = 0
    decaying: int = 0
    risk_rows: int = 0
    duration_s: float = 0.0


def rebuild(
    repo_id: int | None = None,
    conn: psycopg.Connection | None = None,
    force: bool = False,
) -> MiningStats:
    """Run every mining analysis.

    Args:
        repo_id: restrict to one repository.
        conn: reuse an open connection.
        force: re-mine every repository, ignoring watermarks. Without this, only
            repositories whose history moved since their last mining pass are
            re-processed -- clustering, drift and risk are all deterministic
            functions of a repository's own commits, so re-running them on an
            unchanged repo reproduces the same rows at full cost. Mining was 129s
            of a 216s nightly run precisely because it ignored that.
    """

    def _run(c: psycopg.Connection) -> MiningStats:
        started = time.monotonic()
        stats = MiningStats()

        if repo_id is not None:
            targets = [repo_id]
        else:
            stale = "" if force else (
                " AND (r.last_mining_at IS NULL"
                " OR r.last_aggregate_at IS NULL"
                " OR r.last_mining_at < r.last_aggregate_at)"
            )
            targets = [
                int(r[0])
                for r in c.execute(
                    f"""
                    SELECT r.id FROM repo r
                    WHERE r.is_enabled AND r.pair_count > 0 {stale}
                    ORDER BY r.id
                    """
                ).fetchall()
            ]
            if not targets:
                log.info("mining: no repositories have changed since the last pass")
                _refresh_mining_counts(c, stats)
                stats.duration_s = time.monotonic() - started
                return stats

        # Every analysis is scoped to one repository at a time. Running drift
        # unscoped self-joined commit_file across all 4.3M rows at once and
        # spilled ~20 GB of temp files before running the disk out; per-repo the
        # largest single query is bounded by that repo's own history.
        #
        # Note there is deliberately no TRUNCATE here: each per-repo pass deletes
        # only its own rows, so an incremental run leaves untouched repositories'
        # results in place.
        log.info("mining %d repositor%s", len(targets), "y" if len(targets) == 1 else "ies")
        for rid in targets:
            _cluster_repo(c, rid, stats)
            _rebuild_drift(c, rid)
            _rebuild_risk(c, rid)
            c.execute("UPDATE repo SET last_mining_at = now() WHERE id = %s", (rid,))
        _refresh_mining_counts(c, stats)

        stats.duration_s = time.monotonic() - started
        log.info(
            "mining: %d clusters over %d files (%d cross-directory), "
            "%d drift rows (%d emerging, %d decaying), %d risk rows in %.1fs",
            stats.clusters, stats.clustered_files, stats.cross_directory_clusters,
            stats.drift_rows, stats.emerging, stats.decaying, stats.risk_rows,
            stats.duration_s,
        )
        return stats

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def _cluster_repo(conn: psycopg.Connection, repo_id: int, stats: MiningStats) -> None:
    """Find de-facto modules in one repository by label propagation.

    Label propagation is used rather than modularity optimisation because it is
    near-linear in edges, needs no target cluster count, and its instability on
    ambiguous nodes is not a problem here: those nodes genuinely do not belong to
    one module.
    """
    edges = conn.execute(
        """
        SELECT fp.file_a_id, fp.file_b_id, m.npmi
        FROM file_pair fp
        JOIN file_pair_metric m
          ON m.repo_id = fp.repo_id
         AND m.file_a_id = fp.file_a_id AND m.file_b_id = fp.file_b_id
        WHERE fp.repo_id = %(repo)s AND fp.n_ab >= %(support)s AND m.npmi > 0
        """,
        {"repo": repo_id, "support": CLUSTER_MIN_SUPPORT},
    ).fetchall()
    if not edges:
        return

    nodes = sorted({int(e[0]) for e in edges} | {int(e[1]) for e in edges})
    index = {n: i for i, n in enumerate(nodes)}

    # Adjacency as parallel arrays; weights are NPMI, so a strong coupling pulls
    # harder on the label than a weak one.
    src = np.array([index[int(e[0])] for e in edges], dtype=np.int64)
    dst = np.array([index[int(e[1])] for e in edges], dtype=np.int64)
    weight = np.array([float(e[2] or 0.0) for e in edges], dtype=np.float64)

    labels = np.arange(len(nodes), dtype=np.int64)
    for _ in range(LABEL_PROPAGATION_ROUNDS):
        # For each node, accumulate weight per neighbouring label and adopt the
        # heaviest. Done with bincount over a (node, label) composite key so the
        # whole sweep is vectorised.
        n = len(nodes)
        keys = np.concatenate([dst * n + labels[src], src * n + labels[dst]])
        weights = np.concatenate([weight, weight])
        # minlength is n*n and n >= 1 here (the edge list is non-empty), so the
        # reshape below is always well-formed.
        totals = np.bincount(keys, weights=weights, minlength=n * n)
        reshaped = totals.reshape(n, n)
        best = reshaped.argmax(axis=1)
        # Only move a node that actually has an incident edge.
        has_edge = reshaped.max(axis=1) > 0
        new_labels = np.where(has_edge, best, labels)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels

    # Compact labels to dense cluster ids and compute per-cluster properties.
    unique, compact = np.unique(labels, return_inverse=True)
    sizes = np.bincount(compact)

    dirs = {
        int(r[0]): (r[1] or "").split("/")[0]
        for r in conn.execute(
            "SELECT id, dir_path FROM file WHERE id = ANY(%s)", (nodes,)
        ).fetchall()
    }
    cluster_dirs: dict[int, set[str]] = {}
    for i, node in enumerate(nodes):
        cluster_dirs.setdefault(int(compact[i]), set()).add(dirs.get(node, ""))

    # Cohesion: share of a node's coupling weight that stays inside its cluster.
    inside = np.zeros(len(nodes))
    total = np.zeros(len(nodes))
    same = compact[src] == compact[dst]
    np.add.at(total, src, weight)
    np.add.at(total, dst, weight)
    np.add.at(inside, src[same], weight[same])
    np.add.at(inside, dst[same], weight[same])
    cohesion = np.divide(inside, total, out=np.zeros_like(inside), where=total > 0)

    conn.execute("DELETE FROM file_cluster WHERE repo_id = %s", (repo_id,))
    rows = [
        (
            repo_id,
            node,
            int(compact[i]),
            int(sizes[compact[i]]),
            float(cohesion[i]),
            len(cluster_dirs.get(int(compact[i]), {""})),
        )
        for i, node in enumerate(nodes)
        # Singletons are not modules.
        if sizes[compact[i]] >= 2
    ]
    if rows:
        copy_rows(
            "file_cluster",
            ["repo_id", "file_id", "cluster_id", "cluster_size", "cohesion", "dirs_spanned"],
            rows,
            conn=conn,
        )
        stats.clustered_files += len(rows)
        stats.clusters += len({r[2] for r in rows})
        stats.cross_directory_clusters += len({r[2] for r in rows if r[5] > 1})


def _rebuild_drift(conn: psycopg.Connection, repo_id: int) -> None:
    """Compare coupling in a recent window against the preceding history.

    NPMI is recomputed inside each window from that window's own marginals and
    population -- not rescaled from the lifetime figure -- because a pair's
    marginals shift as much as its joint count does, and reusing lifetime
    marginals would attribute a change in activity to a change in coupling.

    Scoped to one repository: see the note in :func:`rebuild`.
    """
    conn.execute("DELETE FROM pair_drift WHERE repo_id = %s", (repo_id,))
    conn.execute(
        """
        WITH windowed AS (
            SELECT c.id AS commit_id,
                   (c.committed_at >= now() - make_interval(days => %(window)s)) AS is_recent
            FROM commit c
            WHERE c.repo_id = %(repo)s AND c.pair_eligible
        ),
        pop AS (
            SELECT count(*) FILTER (WHERE is_recent)     AS n_recent,
                   count(*) FILTER (WHERE NOT is_recent) AS n_historic
            FROM windowed
        ),
        marg AS (
            SELECT cf.file_id,
                   count(*) FILTER (WHERE w.is_recent)     AS m_recent,
                   count(*) FILTER (WHERE NOT w.is_recent) AS m_historic
            FROM commit_file cf
            JOIN windowed w ON w.commit_id = cf.commit_id
            WHERE cf.repo_id = %(repo)s
            GROUP BY cf.file_id
        ),
        joint AS (
            SELECT a.file_id AS a_id, b.file_id AS b_id,
                   count(*) FILTER (WHERE w.is_recent)     AS j_recent,
                   count(*) FILTER (WHERE NOT w.is_recent) AS j_historic
            FROM commit_file a
            JOIN commit_file b
              ON b.commit_id = a.commit_id AND b.file_id > a.file_id
            JOIN windowed w ON w.commit_id = a.commit_id
            WHERE a.repo_id = %(repo)s
            GROUP BY a.file_id, b.file_id
            -- Require evidence in BOTH windows: a pair that simply did not exist
            -- historically is a new file, not a strengthening relationship.
            HAVING count(*) FILTER (WHERE w.is_recent) >= 2
               AND count(*) FILTER (WHERE NOT w.is_recent) >= 2
        )
        INSERT INTO pair_drift (repo_id, file_a_id, file_b_id, window_days,
                                n_ab_recent, n_ab_historic,
                                npmi_recent, npmi_historic, delta, trend)
        SELECT %(repo)s, j.a_id, j.b_id, %(window)s,
               j.j_recent, j.j_historic, r.npmi_recent, r.npmi_historic,
               COALESCE(r.npmi_recent, 0) - COALESCE(r.npmi_historic, 0),
               CASE
                   WHEN COALESCE(r.npmi_recent,0) - COALESCE(r.npmi_historic,0) > 0.15
                       THEN 'emerging'
                   WHEN COALESCE(r.npmi_recent,0) - COALESCE(r.npmi_historic,0) < -0.15
                       THEN 'decaying'
                   ELSE 'stable'
               END
        FROM joint j
        CROSS JOIN pop p
        JOIN marg ma ON ma.file_id = j.a_id
        JOIN marg mb ON mb.file_id = j.b_id
        CROSS JOIN LATERAL (
            SELECT
              CASE WHEN j.j_recent > 0 AND p.n_recent > 0
                        AND ma.m_recent > 0 AND mb.m_recent > 0
                        AND j.j_recent < p.n_recent
                   THEN (ln((j.j_recent::numeric * p.n_recent)
                            / (ma.m_recent::numeric * mb.m_recent)) / ln(2))
                        / (-ln(j.j_recent::numeric / p.n_recent) / ln(2))
              END AS npmi_recent,
              CASE WHEN j.j_historic > 0 AND p.n_historic > 0
                        AND ma.m_historic > 0 AND mb.m_historic > 0
                        AND j.j_historic < p.n_historic
                   THEN (ln((j.j_historic::numeric * p.n_historic)
                            / (ma.m_historic::numeric * mb.m_historic)) / ln(2))
                        / (-ln(j.j_historic::numeric / p.n_historic) / ln(2))
              END AS npmi_historic
        ) r
        ON CONFLICT (repo_id, file_a_id, file_b_id) DO NOTHING
        """,
        {"repo": repo_id, "window": DRIFT_WINDOW_DAYS},
    )


def _rebuild_risk(conn: psycopg.Connection, repo_id: int) -> None:
    """Score each file's maintenance risk within one repository.

    Components are percentiles *within the repository*, so the composite is
    comparable across a 300-commit utility and a 14,000-commit monolith. Churn
    and coupling multiply; ownership concentration only lifts. A file nobody
    touches is not risky however concentrated its ownership, whereas a busy,
    well-connected file is risky even when widely shared.

    The whole product is then weighted by ``n / (n + RISK_EVIDENCE_K)`` so that a
    small repository's busiest file, which necessarily sits at the 100th
    percentile, cannot outrank a genuine hotspot on five commits of evidence.
    """
    conn.execute("DELETE FROM file_risk WHERE repo_id = %s", (repo_id,))
    conn.execute(
        """
        WITH base AS (
            SELECT f.id AS file_id, f.repo_id, f.change_count, f.author_count,
                   f.last_change_at,
                   (SELECT count(*) FROM file_pair p
                     WHERE p.repo_id = f.repo_id
                       AND (p.file_a_id = f.id OR p.file_b_id = f.id)) AS partner_count
            FROM file f
            WHERE f.repo_id = %(repo)s AND f.change_count > 0
        ),
        shares AS (
            SELECT af.file_id,
                   af.n_commits::numeric
                     / NULLIF(sum(af.n_commits) OVER (PARTITION BY af.file_id), 0) AS share
            FROM author_file af
            WHERE af.repo_id = %(repo)s
        ),
        hhi AS (
            -- Herfindahl-Hirschman: sum of squared author shares per file.
            SELECT file_id, sum(share * share) AS ownership_hhi
            FROM shares GROUP BY file_id
        ),
        pct AS (
            SELECT b.*, h.ownership_hhi,
                   percent_rank() OVER (ORDER BY b.change_count)   AS churn_pct,
                   percent_rank() OVER (ORDER BY b.partner_count)  AS coupling_pct
            FROM base b LEFT JOIN hhi h ON h.file_id = b.file_id
        )
        INSERT INTO file_risk (file_id, repo_id, churn_pct, coupling_pct,
                               ownership_hhi, effective_authors, author_count,
                               partner_count, change_count, days_since_change,
                               risk_score)
        SELECT file_id, repo_id, churn_pct, coupling_pct, ownership_hhi,
               CASE WHEN ownership_hhi > 0 THEN 1.0 / ownership_hhi END,
               author_count, partner_count, change_count,
               CASE WHEN last_change_at IS NOT NULL
                    THEN EXTRACT(DAY FROM (now() - last_change_at))::int END,
               -- churn x coupling, lifted by ownership concentration, then
               -- shrunk toward zero when the evidence is thin.
               (churn_pct * coupling_pct)
                 * (1 + COALESCE(ownership_hhi, 0))
                 * (change_count::numeric / (change_count + %(k)s))
        FROM pct
        ON CONFLICT (file_id) DO NOTHING
        """,
        {"repo": repo_id, "k": RISK_EVIDENCE_K},
    )


def _refresh_mining_counts(conn: psycopg.Connection, stats: MiningStats) -> None:
    """Fill in the aggregate counters after all per-repo passes complete."""
    row = conn.execute(
        """
        SELECT count(*), count(*) FILTER (WHERE trend='emerging'),
               count(*) FILTER (WHERE trend='decaying')
        FROM pair_drift
        """
    ).fetchone()
    stats.drift_rows, stats.emerging, stats.decaying = (
        int(row[0]), int(row[1]), int(row[2])
    )
    stats.risk_rows = int(conn.execute("SELECT count(*) FROM file_risk").fetchone()[0])


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def cross_directory_modules(repo_id: int, limit: int = 20) -> list[dict]:
    """De-facto modules whose files span more than one top-level directory."""
    return query(
        """
        SELECT fc.cluster_id, fc.cluster_size, fc.dirs_spanned,
               round(avg(fc.cohesion)::numeric, 3) AS avg_cohesion,
               array_agg(DISTINCT split_part(f.dir_path, '/', 1)
                         ORDER BY split_part(f.dir_path, '/', 1)) AS directories,
               (array_agg(f.path ORDER BY f.change_count DESC))[1:6] AS sample_files
        FROM file_cluster fc
        JOIN file f ON f.id = fc.file_id
        WHERE fc.repo_id = %(repo)s AND fc.dirs_spanned > 1 AND fc.cluster_size >= 3
        GROUP BY fc.cluster_id, fc.cluster_size, fc.dirs_spanned
        ORDER BY fc.cluster_size DESC
        LIMIT %(limit)s
        """,
        {"repo": repo_id, "limit": limit},
    )


def drifting_pairs(
    repo_id: int | None = None, trend: str = "emerging", limit: int = 25
) -> list[dict]:
    """Pairs whose coupling is strengthening or decaying."""
    clause = "WHERE d.trend = %(trend)s"
    params: dict = {"trend": trend, "limit": limit}
    if repo_id is not None:
        clause += " AND d.repo_id = %(repo)s"
        params["repo"] = repo_id
    order = "d.delta DESC" if trend == "emerging" else "d.delta ASC"
    return query(
        f"""
        SELECT d.*, fa.path AS path_a, fb.path AS path_b, r.name AS repo
        FROM pair_drift d
        JOIN file fa ON fa.id = d.file_a_id
        JOIN file fb ON fb.id = d.file_b_id
        JOIN repo r ON r.id = d.repo_id
        {clause}
        ORDER BY {order}
        LIMIT %(limit)s
        """,
        params,
    )


def risky_files(repo_id: int | None = None, limit: int = 25) -> list[dict]:
    """Files where churn, coupling and concentrated ownership coincide."""
    clause = "WHERE fr.change_count >= 5"
    params: dict = {"limit": limit}
    if repo_id is not None:
        clause += " AND fr.repo_id = %(repo)s"
        params["repo"] = repo_id
    return query(
        f"""
        SELECT fr.*, f.path, f.extension, r.name AS repo, r.full_name,
               (SELECT a.display_name FROM author_file af
                 JOIN author a ON a.id = af.author_id
                WHERE af.file_id = fr.file_id
                ORDER BY af.n_commits DESC LIMIT 1) AS top_author
        FROM file_risk fr
        JOIN file f ON f.id = fr.file_id
        JOIN repo r ON r.id = fr.repo_id
        {clause}
        ORDER BY fr.risk_score DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params,
    )
