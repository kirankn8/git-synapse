"""Read-side queries backing the REST API, the MCP server and the UI.

Direction matters
-----------------
Pairs are stored once, canonically ordered with ``file_a_id < file_b_id``. When
a caller asks "what changes with X?", X may be stored on either side, so the
symmetric measures can be read as-is but the *asymmetric* ones must be swapped:
``confidence_ab`` is ``P(b | a)``, which is only "probability the partner
changes given X changed" when X happens to be the ``a`` side. Getting this
backwards silently reports the wrong conditional, so the swap is done in SQL via
a CASE on which column matched.
"""

from __future__ import annotations

import logging
from typing import Any

from git_synapse.config import get_config
from git_synapse.db.engine import query, query_one, scalar
from git_synapse.stats.registry import BY_KEY, DEFAULT_MEASURE, MEASURES, resolve

log = logging.getLogger(__name__)

#: Measure keys that may be used for ordering. Restricted to the registry so a
#: caller-supplied string can never be interpolated into SQL unchecked.
SORTABLE = {spec.key for spec in MEASURES} | {"n_ab", "w_ab", "last_co_change"}


def _clamp_limit(limit: int | None) -> int:
    cfg = get_config().analysis
    if limit is None:
        return cfg.default_limit
    return max(1, min(int(limit), cfg.max_limit))


def _safe_order(measure: str) -> str:
    """Validate an ordering key against the registry allowlist.

    Raises:
        KeyError: if the key is not a known measure or pair column.
    """
    key = (measure or DEFAULT_MEASURE).strip().lower()
    if key in {"n_ab", "w_ab", "last_co_change"}:
        return key
    return resolve(key).key


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def list_repos(
    search: str | None = None,
    language: str | None = None,
    status: str | None = None,
    order_by: str = "commit_count",
    descending: bool = True,
    limit: int | None = 500,
    offset: int = 0,
) -> list[dict]:
    """List repositories with their ingest state and history summary."""
    allowed = {
        "commit_count", "file_count", "pair_count", "author_count", "name",
        "stargazers", "last_commit_at", "github_pushed_at", "disk_usage_kb",
        "total_insertions", "last_ingest_at",
    }
    column = order_by if order_by in allowed else "commit_count"
    direction = "DESC NULLS LAST" if descending else "ASC NULLS LAST"

    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": _clamp_limit(limit), "offset": max(offset, 0)}
    if search:
        clauses.append("(full_name ILIKE %(search)s OR description ILIKE %(search)s)")
        params["search"] = f"%{search}%"
    if language:
        clauses.append("primary_language = %(language)s")
        params["language"] = language
    if status:
        clauses.append("ingest_status = %(status)s")
        params["status"] = status

    return query(
        f"""
        SELECT id, full_name, owner, name, description, html_url, primary_language,
               topics, is_private, is_fork, is_archived, stargazers, forks_count,
               open_issues, license_spdx, visibility, default_branch, disk_usage_kb,
               mirror_size_kb, clone_mode, has_churn, ingest_status, ingest_error,
               commit_count, pair_population, file_count, author_count, pair_count,
               total_insertions, total_deletions, first_commit_at, last_commit_at,
               github_created_at, github_pushed_at, last_ingest_at, last_aggregate_at
        FROM repo
        WHERE {' AND '.join(clauses)}
        ORDER BY {column} {direction}
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )


def get_repo(repo_id: int) -> dict | None:
    """Full record for one repository, including the raw GitHub payload."""
    return query_one("SELECT * FROM repo WHERE id = %s", (repo_id,))


def repo_languages() -> list[dict]:
    """Distinct primary languages with repo counts, for the UI filter."""
    return query(
        """
        SELECT primary_language AS language, count(*) AS n
        FROM repo WHERE primary_language IS NOT NULL
        GROUP BY 1 ORDER BY n DESC
        """
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def search_files(
    term: str | None = None,
    repo_id: int | None = None,
    extension: str | None = None,
    min_changes: int = 0,
    order_by: str = "change_count",
    limit: int | None = 50,
    offset: int = 0,
) -> list[dict]:
    """Search files by path substring, optionally scoped to one repository."""
    allowed = {"change_count", "path", "last_change_at", "insertions", "author_count"}
    column = order_by if order_by in allowed else "change_count"
    direction = "ASC" if column == "path" else "DESC NULLS LAST"

    clauses = ["f.change_count >= %(min_changes)s"]
    params: dict[str, Any] = {
        "min_changes": max(min_changes, 0),
        "limit": _clamp_limit(limit),
        "offset": max(offset, 0),
    }
    if term:
        clauses.append("f.path ILIKE %(term)s")
        params["term"] = f"%{term}%"
    if repo_id:
        clauses.append("f.repo_id = %(repo_id)s")
        params["repo_id"] = repo_id
    if extension:
        clauses.append("f.extension = %(extension)s")
        params["extension"] = extension

    return query(
        f"""
        SELECT f.id, f.repo_id, r.full_name AS repo, f.path, f.dir_path, f.basename,
               f.extension, f.depth, f.is_deleted, f.change_count, f.pair_change_count,
               f.insertions, f.deletions, f.author_count, f.first_change_at,
               f.last_change_at
        FROM file f
        JOIN repo r ON r.id = f.repo_id
        WHERE {' AND '.join(clauses)}
        ORDER BY {column} {direction}
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )


def get_file(file_id: int) -> dict | None:
    """One file with its repo context and marginal counts."""
    return query_one(
        """
        SELECT f.*, r.full_name AS repo, r.pair_population, r.has_churn,
               r.html_url AS repo_url, r.default_branch
        FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE f.id = %s
        """,
        (file_id,),
    )


def resolve_file(repo: str, path: str) -> dict | None:
    """Find a file by repository name and path, following renames.

    Accepts a bare repo name or ``owner/name``, and falls back to the alias
    table so a caller using a path that has since been renamed still resolves.
    """
    row = query_one(
        """
        SELECT f.id FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE (r.full_name = %(repo)s OR r.name = %(repo)s) AND f.path = %(path)s
        LIMIT 1
        """,
        {"repo": repo, "path": path},
    )
    if row is None:
        row = query_one(
            """
            SELECT fa.file_id AS id FROM file_alias fa
            JOIN repo r ON r.id = fa.repo_id
            WHERE (r.full_name = %(repo)s OR r.name = %(repo)s) AND fa.old_path = %(path)s
            LIMIT 1
            """,
            {"repo": repo, "path": path},
        )
    return get_file(int(row["id"])) if row else None


def file_extensions(repo_id: int | None = None) -> list[dict]:
    """Extension histogram, for the UI filter."""
    clause = "WHERE extension IS NOT NULL"
    params: dict[str, Any] = {}
    if repo_id:
        clause += " AND repo_id = %(repo_id)s"
        params["repo_id"] = repo_id
    return query(
        f"SELECT extension, count(*) AS n FROM file {clause} GROUP BY 1 ORDER BY n DESC LIMIT 60",
        params,
    )


# ---------------------------------------------------------------------------
# Coupling: the core query
# ---------------------------------------------------------------------------

#: Every measure column, aliased so the caller gets them all in one row.
_METRIC_COLUMNS = ", ".join(f"m.{spec.key}" for spec in MEASURES)


def _oriented_order(measure: str) -> tuple[str, str, str]:
    """Order and filter columns for queries that union both pair orientations.

    Those queries flip confidence into ``confidence_out``/``confidence_in`` so it
    always reads outward from the entity asked about. Ranking on the stored
    ``confidence_ab`` instead would rank by whichever direction the pair happens
    to be stored in, which is arbitrary, so the directional measures map onto the
    flipped aliases.

    Returns the outer order column and the raw column to filter on in the A-side
    and B-side branches, which differ for exactly those two measures.
    """
    order = _safe_order(measure)
    if order == "confidence_ab":
        return "confidence_out", "confidence_ab", "confidence_ba"
    if order == "confidence_ba":
        return "confidence_in", "confidence_ba", "confidence_ab"
    return order, order, order


def coupled_files(
    file_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int | None = 25,
    min_support: int = 1,
    min_score: float | None = None,
) -> list[dict]:
    """Files that historically change together with ``file_id``, ranked.

    This is the query a coding agent asks: "I am editing X, what else has
    always had to change?"

    The pair table stores each unordered pair once, so both orientations are
    unioned. ``confidence`` is flipped where needed so it always reads as
    ``P(partner changes | this file changed)``.
    """
    order, order_a, order_b = _oriented_order(measure)
    params: dict[str, Any] = {
        "file_id": file_id,
        "limit": _clamp_limit(limit),
        "min_support": max(min_support, 1),
    }

    score_a = score_b = ""
    if min_score is not None:
        score_a = f"AND m.{order_a} >= %(min_score)s"
        score_b = f"AND m.{order_b} >= %(min_score)s"
        params["min_score"] = min_score

    return query(
        f"""
        WITH partners AS (
            SELECT m.file_b_id AS other_id,
                   m.confidence_ab AS confidence_out,
                   m.confidence_ba AS confidence_in,
                   m.n_a AS n_this, m.n_b AS n_other,
                   {_METRIC_COLUMNS}, m.n_ab, m.n_a, m.n_b, m.n_total
            FROM file_pair_metric m
            WHERE m.file_a_id = %(file_id)s AND m.n_ab >= %(min_support)s {score_a}
            UNION ALL
            SELECT m.file_a_id AS other_id,
                   -- This file is the B side, so P(partner | this) is ba and the
                   -- partner's own change count is n_a, not n_b.
                   m.confidence_ba AS confidence_out,
                   m.confidence_ab AS confidence_in,
                   m.n_b AS n_this, m.n_a AS n_other,
                   {_METRIC_COLUMNS}, m.n_ab, m.n_a, m.n_b, m.n_total
            FROM file_pair_metric m
            WHERE m.file_b_id = %(file_id)s AND m.n_ab >= %(min_support)s {score_b}
        )
        SELECT p.*, p.{order} AS score,
               f.path, f.dir_path, f.basename, f.extension, f.repo_id,
               f.change_count, f.is_deleted, r.full_name AS repo,
               fp.last_co_change, fp.first_co_change, fp.distinct_authors, fp.w_ab,
               -- Recency and trend, so a caller cannot mistake a completed
               -- refactor for live coupling. A lifetime score says nothing about
               -- whether the relationship still holds, and reading a raw
               -- timestamp to work that out is a step callers skip.
               CASE WHEN fp.last_co_change IS NOT NULL
                    THEN EXTRACT(DAY FROM (now() - fp.last_co_change))::int
               END AS days_since_co_change,
               d.trend,
               d.n_ab_recent,
               d.n_ab_historic,
               d.delta AS trend_delta
        FROM partners p
        JOIN file f ON f.id = p.other_id
        JOIN repo r ON r.id = f.repo_id
        LEFT JOIN file_pair fp
               ON fp.repo_id = f.repo_id
              AND fp.file_a_id = LEAST(%(file_id)s, p.other_id)
              AND fp.file_b_id = GREATEST(%(file_id)s, p.other_id)
        LEFT JOIN pair_drift d
               ON d.repo_id = f.repo_id
              AND d.file_a_id = LEAST(%(file_id)s, p.other_id)
              AND d.file_b_id = GREATEST(%(file_id)s, p.other_id)
        ORDER BY p.{order} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params,
    )


def coupled_directories(
    dir_id: int, measure: str = DEFAULT_MEASURE, limit: int | None = 25
) -> list[dict]:
    """Directories that change together with ``dir_id``, ranked."""
    order, _, _ = _oriented_order(measure)
    metric_cols = ", ".join(f"m.{spec.key}" for spec in MEASURES)
    return query(
        f"""
        WITH partners AS (
            SELECT m.dir_b_id AS other_id,
                   m.confidence_ab AS confidence_out, m.confidence_ba AS confidence_in,
                   m.n_a AS n_this, m.n_b AS n_other,
                   {metric_cols}, m.n_ab, m.n_a, m.n_b, m.n_total
            FROM dir_pair_metric m WHERE m.dir_a_id = %(dir_id)s
            UNION ALL
            SELECT m.dir_a_id AS other_id,
                   m.confidence_ba AS confidence_out, m.confidence_ab AS confidence_in,
                   m.n_b AS n_this, m.n_a AS n_other,
                   {metric_cols}, m.n_ab, m.n_a, m.n_b, m.n_total
            FROM dir_pair_metric m WHERE m.dir_b_id = %(dir_id)s
        )
        SELECT p.*, p.{order} AS score,
               d.path, d.depth, d.file_count, d.change_count, d.repo_id
        FROM partners p JOIN directory d ON d.id = p.other_id
        ORDER BY p.{order} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        {"dir_id": dir_id, "limit": _clamp_limit(limit)},
    )


def pair_detail(file_a_id: int, file_b_id: int) -> dict | None:
    """Everything known about one pair: contingency cells and all measures."""
    lo, hi = sorted((file_a_id, file_b_id))
    row = query_one(
        f"""
        SELECT m.*, fa.path AS path_a, fb.path AS path_b, r.full_name AS repo,
               fp.first_co_change, fp.last_co_change, fp.distinct_authors, fp.w_ab
        FROM file_pair_metric m
        JOIN file fa ON fa.id = m.file_a_id
        JOIN file fb ON fb.id = m.file_b_id
        JOIN repo r ON r.id = m.repo_id
        LEFT JOIN file_pair fp ON fp.repo_id = m.repo_id
             AND fp.file_a_id = m.file_a_id AND fp.file_b_id = m.file_b_id
        WHERE m.file_a_id = %s AND m.file_b_id = %s
        """,
        (lo, hi),
    )
    if row is None:
        return None

    # The pair is stored once, canonicalised by id. Returning it in storage order
    # silently transposed the caller's arguments, so confidence_ab read as the
    # reverse conditional for half of all pairs.
    if (file_a_id, file_b_id) != (lo, hi):
        for x, y in (
            ("file_a_id", "file_b_id"), ("path_a", "path_b"),
            ("n_a", "n_b"), ("confidence_ab", "confidence_ba"),
        ):
            row[x], row[y] = row[y], row[x]

    a, n_a, n_b, n = row["n_ab"], row["n_a"], row["n_b"], row["n_total"]
    row["cells"] = {
        "a": a, "b": n_a - a, "c": n_b - a, "d": n - n_a - n_b + a,
        "n_a": n_a, "n_b": n_b, "n_total": n,
        "expected": (n_a * n_b / n) if n else 0.0,
    }
    return row


def co_change_commits(file_a_id: int, file_b_id: int, limit: int = 25) -> list[dict]:
    """The actual commits in which both files changed -- the evidence behind a score."""
    return query(
        """
        SELECT c.id, c.sha, c.subject, c.committed_at, c.n_files,
               c.insertions, c.deletions, a.display_name AS author, a.email
        FROM commit c
        JOIN commit_file cfa ON cfa.commit_id = c.id AND cfa.file_id = %(a)s
        JOIN commit_file cfb ON cfb.commit_id = c.id AND cfb.file_id = %(b)s
        LEFT JOIN author a ON a.id = c.author_id
        ORDER BY c.committed_at DESC
        LIMIT %(limit)s
        """,
        {"a": file_a_id, "b": file_b_id, "limit": _clamp_limit(limit)},
    )


def file_commits(file_id: int, limit: int = 50) -> list[dict]:
    """Commit history for one file."""
    return query(
        """
        SELECT c.id, c.sha, c.subject, c.committed_at, c.n_files,
               cf.change_type, cf.insertions, cf.deletions, cf.old_path,
               a.display_name AS author, a.email
        FROM commit_file cf
        JOIN commit c ON c.id = cf.commit_id
        LEFT JOIN author a ON a.id = c.author_id
        WHERE cf.file_id = %(file_id)s
        ORDER BY c.committed_at DESC
        LIMIT %(limit)s
        """,
        {"file_id": file_id, "limit": _clamp_limit(limit)},
    )


def file_authors(file_id: int, limit: int = 20) -> list[dict]:
    """Who has actually worked on this file, most active first."""
    return query(
        """
        SELECT a.id, a.display_name, a.email, af.n_commits, af.insertions,
               af.deletions, af.first_at, af.last_at
        FROM author_file af JOIN author a ON a.id = af.author_id
        WHERE af.file_id = %(file_id)s
        ORDER BY af.n_commits DESC
        LIMIT %(limit)s
        """,
        {"file_id": file_id, "limit": _clamp_limit(limit)},
    )


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def coupling_graph(
    repo_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = 150,
    min_support: int = 2,
    center_file_id: int | None = None,
    min_score: float | None = None,
) -> dict:
    """Build a node/edge graph of the strongest couplings, for visualisation.

    Args:
        repo_id: repository to graph.
        measure: which measure ranks the edges.
        limit: maximum edges. Nodes are whatever those edges touch.
        min_support: ignore pairs with fewer co-changes than this.
        center_file_id: when set, return only the neighbourhood of this file.
        min_score: optional score floor.

    Returns:
        ``{"nodes": [...], "edges": [...], "measure": ..., "stats": {...}}``
    """
    order = _safe_order(measure)
    params: dict[str, Any] = {
        "repo_id": repo_id,
        "limit": max(1, min(int(limit), 2000)),
        "min_support": max(min_support, 1),
    }

    filters = ["m.repo_id = %(repo_id)s", "m.n_ab >= %(min_support)s"]
    if min_score is not None:
        filters.append(f"m.{order} >= %(min_score)s")
        params["min_score"] = min_score
    if center_file_id:
        filters.append("(m.file_a_id = %(center)s OR m.file_b_id = %(center)s)")
        params["center"] = center_file_id

    edges = query(
        f"""
        SELECT m.file_a_id AS source, m.file_b_id AS target,
               m.{order} AS score, m.n_ab, m.npmi, m.jaccard,
               m.log_likelihood_ratio, m.confidence_ab, m.confidence_ba
        FROM file_pair_metric m
        WHERE {' AND '.join(filters)}
        ORDER BY m.{order} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params,
    )

    node_ids = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
    nodes: list[dict] = []
    if node_ids:
        nodes = query(
            """
            SELECT f.id, f.path, f.basename, f.dir_path, f.extension,
                   f.change_count, f.is_deleted
            FROM file f WHERE f.id = ANY(%(ids)s)
            """,
            {"ids": node_ids},
        )

    return {
        "measure": order,
        "nodes": nodes,
        "edges": edges,
        "stats": {"node_count": len(nodes), "edge_count": len(edges)},
    }


# ---------------------------------------------------------------------------
# Overview / hotspots / runs
# ---------------------------------------------------------------------------


def overview() -> dict:
    """Headline counts for the dashboard."""
    row = query_one(
        """
        SELECT
          (SELECT count(*) FROM repo)                                AS repos,
          (SELECT count(*) FROM repo WHERE ingest_status='ready')    AS repos_ready,
          (SELECT count(*) FROM repo WHERE ingest_status='failed')   AS repos_failed,
          (SELECT count(*) FROM commit)                              AS commits,
          (SELECT count(*) FROM commit_file)                         AS file_changes,
          (SELECT count(*) FROM file)                                AS files,
          (SELECT count(*) FROM directory)                           AS directories,
          (SELECT count(*) FROM author)                              AS authors,
          (SELECT count(*) FROM file_pair)                           AS file_pairs,
          (SELECT count(*) FROM dir_pair)                            AS dir_pairs,
          (SELECT coalesce(sum(mirror_size_kb),0) FROM repo)         AS mirror_kb,
          (SELECT min(committed_at) FROM commit)                     AS first_commit_at,
          (SELECT max(committed_at) FROM commit)                     AS last_commit_at
        """
    )
    return row or {}


def hotspots(repo_id: int | None = None, limit: int = 25) -> list[dict]:
    """Most-changed files -- the churn leaders."""
    clause = "WHERE f.change_count > 0"
    params: dict[str, Any] = {"limit": _clamp_limit(limit)}
    if repo_id:
        clause += " AND f.repo_id = %(repo_id)s"
        params["repo_id"] = repo_id
    return query(
        f"""
        SELECT f.id, f.path, f.repo_id, r.full_name AS repo, f.change_count,
               f.insertions, f.deletions, f.author_count, f.last_change_at,
               (SELECT count(*) FROM file_pair fp
                 WHERE fp.file_a_id = f.id OR fp.file_b_id = f.id) AS partner_count
        FROM file f JOIN repo r ON r.id = f.repo_id
        {clause}
        ORDER BY f.change_count DESC
        LIMIT %(limit)s
        """,
        params,
    )


def strongest_pairs(
    repo_id: int | None = None,
    measure: str = DEFAULT_MEASURE,
    limit: int = 50,
    min_support: int = 3,
) -> list[dict]:
    """Highest-scoring pairs, optionally across the whole org."""
    order = _safe_order(measure)
    clause = "WHERE m.n_ab >= %(min_support)s"
    params: dict[str, Any] = {
        "limit": _clamp_limit(limit),
        "min_support": max(min_support, 1),
    }
    if repo_id:
        clause += " AND m.repo_id = %(repo_id)s"
        params["repo_id"] = repo_id
    return query(
        f"""
        SELECT m.repo_id, r.full_name AS repo, m.file_a_id, m.file_b_id,
               fa.path AS path_a, fb.path AS path_b,
               m.n_ab, m.n_a, m.n_b, m.n_total, m.{order} AS score,
               m.npmi, m.jaccard, m.log_likelihood_ratio, m.phi,
               m.association_strength, m.confidence_ab, m.confidence_ba
        FROM file_pair_metric m
        JOIN file fa ON fa.id = m.file_a_id
        JOIN file fb ON fb.id = m.file_b_id
        JOIN repo r ON r.id = m.repo_id
        {clause}
        ORDER BY m.{order} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params,
    )


def directories(repo_id: int, limit: int = 200) -> list[dict]:
    """Directory tree for a repository, busiest first."""
    return query(
        """
        SELECT id, path, depth, file_count, change_count, pair_change_count,
               first_change_at, last_change_at
        FROM directory WHERE repo_id = %(repo_id)s
        ORDER BY change_count DESC LIMIT %(limit)s
        """,
        {"repo_id": repo_id, "limit": _clamp_limit(limit)},
    )


def recent_runs(limit: int = 20) -> list[dict]:
    """Ingest run history for the status page."""
    return query(
        """
        SELECT id, kind, trigger, status, started_at, finished_at, duration_s,
               repos_total, repos_ok, repos_failed, commits_added, pairs_written, error
        FROM ingest_run ORDER BY started_at DESC LIMIT %(limit)s
        """,
        {"limit": _clamp_limit(limit)},
    )


def run_detail(run_id: int) -> dict | None:
    """One run plus its per-repository breakdown."""
    run = query_one("SELECT * FROM ingest_run WHERE id = %s", (run_id,))
    if run is None:
        return None
    run["repos"] = query(
        """
        SELECT rr.repo_id, r.full_name, rr.status, rr.commits_added,
               rr.duration_s, rr.error
        FROM ingest_run_repo rr JOIN repo r ON r.id = rr.repo_id
        WHERE rr.run_id = %s
        ORDER BY rr.duration_s DESC NULLS LAST
        """,
        (run_id,),
    )
    return run


def measure_catalog() -> list[dict]:
    """The registry, serialised for the API and the UI's metric picker."""
    return [
        {
            "key": s.key,
            "label": s.label,
            "family": s.family,
            "formula": s.formula,
            "summary": s.summary,
            "detail": s.detail,
            "lower": s.lower,
            "upper": s.upper,
            "signed": s.signed,
            "neutral": s.neutral,
            "is_significance": s.is_significance,
            "rare_item_bias": s.rare_item_bias,
            "saturates_on_sparse": s.saturates_on_sparse,
            "recommended": s.recommended,
            "aliases": list(s.aliases),
        }
        for s in MEASURES
    ]


# ---------------------------------------------------------------------------
# Cross-repository coupling
# ---------------------------------------------------------------------------

_XREPO_METRICS = ", ".join(f"m.{spec.key}" for spec in MEASURES)


def crossrepo_overview() -> dict:
    """Headline counts for the cross-repo layer."""
    return query_one(
        """
        SELECT
          (SELECT count(*) FROM change_set)                                AS change_sets,
          (SELECT count(*) FROM change_set WHERE signal='ticket')          AS ticket_sets,
          (SELECT count(*) FROM change_set WHERE signal='temporal')        AS temporal_sets,
          (SELECT count(*) FROM change_set WHERE pair_eligible)            AS eligible_sets,
          (SELECT count(*) FROM change_set WHERE n_repos > 1
                                             AND pair_eligible)            AS multi_repo_sets,
          (SELECT count(*) FROM repo_pair)                                 AS repo_pairs,
          (SELECT count(*) FROM xrepo_file_pair)                           AS file_pairs,
          (SELECT max(n_repos) FROM change_set)                            AS widest_set
        """
    ) or {}


def repo_partners(
    repo_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int | None = 25,
    min_support: int = 2,
) -> list[dict]:
    """Repositories that change together with ``repo_id``, ranked.

    ``confidence_out`` always reads as ``P(partner changes | this repo changed)``,
    with the stored direction flipped when this repo is the ``b`` side -- the same
    correction :func:`coupled_files` applies.
    """
    order, _, _ = _oriented_order(measure)
    return query(
        f"""
        WITH partners AS (
            SELECT m.repo_b_id AS other_id,
                   m.confidence_ab AS confidence_out,
                   m.confidence_ba AS confidence_in,
                   {_XREPO_METRICS}, m.n_ab, m.n_a, m.n_b, m.n_total,
                   p.n_ab_ticket, p.last_co_change, p.first_co_change,
                   p.distinct_authors, p.w_ab
            FROM repo_pair_metric m
            JOIN repo_pair p ON p.repo_a_id = m.repo_a_id AND p.repo_b_id = m.repo_b_id
            WHERE m.repo_a_id = %(repo_id)s AND m.n_ab >= %(min_support)s
            UNION ALL
            SELECT m.repo_a_id AS other_id,
                   m.confidence_ba AS confidence_out,
                   m.confidence_ab AS confidence_in,
                   {_XREPO_METRICS}, m.n_ab, m.n_a, m.n_b, m.n_total,
                   p.n_ab_ticket, p.last_co_change, p.first_co_change,
                   p.distinct_authors, p.w_ab
            FROM repo_pair_metric m
            JOIN repo_pair p ON p.repo_a_id = m.repo_a_id AND p.repo_b_id = m.repo_b_id
            WHERE m.repo_b_id = %(repo_id)s AND m.n_ab >= %(min_support)s
        )
        SELECT pt.*, r.full_name, r.name, r.primary_language, r.description,
               r.commit_count,
               -- Share of the joint evidence that came from explicit ticket
               -- links rather than temporal proximity.
               CASE WHEN pt.n_ab > 0
                    THEN pt.n_ab_ticket::float / pt.n_ab ELSE 0 END AS ticket_ratio
        FROM partners pt
        JOIN repo r ON r.id = pt.other_id
        ORDER BY pt.{order} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        {"repo_id": repo_id, "limit": _clamp_limit(limit), "min_support": max(min_support, 1)},
    )


def crossrepo_file_partners(
    file_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int | None = 25,
    min_support: int = 2,
) -> list[dict]:
    """Files in *other* repositories that change together with ``file_id``.

    This is the actionable cross-repo answer: "the contracts spec changed, so this
    telemetry handler probably needs updating too".
    """
    order, _, _ = _oriented_order(measure)
    return query(
        f"""
        WITH partners AS (
            SELECT m.file_b_id AS other_id,
                   m.confidence_ab AS confidence_out,
                   m.confidence_ba AS confidence_in,
                   {_XREPO_METRICS}, m.n_ab, m.n_a, m.n_b, m.n_total,
                   p.n_ab_ticket, p.last_co_change
            FROM xrepo_file_pair_metric m
            JOIN xrepo_file_pair p ON p.file_a_id = m.file_a_id AND p.file_b_id = m.file_b_id
            WHERE m.file_a_id = %(file_id)s AND m.n_ab >= %(min_support)s
            UNION ALL
            SELECT m.file_a_id AS other_id,
                   m.confidence_ba AS confidence_out,
                   m.confidence_ab AS confidence_in,
                   {_XREPO_METRICS}, m.n_ab, m.n_a, m.n_b, m.n_total,
                   p.n_ab_ticket, p.last_co_change
            FROM xrepo_file_pair_metric m
            JOIN xrepo_file_pair p ON p.file_a_id = m.file_a_id AND p.file_b_id = m.file_b_id
            WHERE m.file_b_id = %(file_id)s AND m.n_ab >= %(min_support)s
        )
        SELECT pt.*, f.path, f.basename, f.dir_path, f.extension, f.repo_id,
               f.change_count, f.is_deleted, r.full_name AS repo, r.name AS repo_name,
               CASE WHEN pt.n_ab > 0
                    THEN pt.n_ab_ticket::float / pt.n_ab ELSE 0 END AS ticket_ratio
        FROM partners pt
        JOIN file f ON f.id = pt.other_id
        JOIN repo r ON r.id = f.repo_id
        ORDER BY pt.{order} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        {"file_id": file_id, "limit": _clamp_limit(limit), "min_support": max(min_support, 1)},
    )


def top_crossrepo_pairs(
    measure: str = DEFAULT_MEASURE,
    limit: int | None = 50,
    min_support: int = 5,
    level: str = "repo",
) -> list[dict]:
    """Strongest cross-repo couplings, at repo or file granularity."""
    order = _safe_order(measure)
    params = {"limit": _clamp_limit(limit), "min_support": max(min_support, 1)}

    if level == "repo":
        return query(
            f"""
            SELECT m.repo_a_id, m.repo_b_id,
                   ra.name AS repo_a, rb.name AS repo_b,
                   ra.full_name AS full_a, rb.full_name AS full_b,
                   m.n_ab, m.n_a, m.n_b, m.n_total, p.n_ab_ticket,
                   m.{order} AS score, m.npmi, m.jaccard, m.phi,
                   m.log_likelihood_ratio, m.association_strength,
                   m.confidence_ab, m.confidence_ba, p.last_co_change,
                   p.distinct_authors,
                   CASE WHEN m.n_ab > 0
                        THEN p.n_ab_ticket::float / m.n_ab ELSE 0 END AS ticket_ratio
            FROM repo_pair_metric m
            JOIN repo_pair p ON p.repo_a_id = m.repo_a_id AND p.repo_b_id = m.repo_b_id
            JOIN repo ra ON ra.id = m.repo_a_id
            JOIN repo rb ON rb.id = m.repo_b_id
            WHERE m.n_ab >= %(min_support)s
            ORDER BY m.{order} DESC NULLS LAST
            LIMIT %(limit)s
            """,
            params,
        )

    return query(
        f"""
        SELECT m.file_a_id, m.file_b_id, m.repo_a_id, m.repo_b_id,
               fa.path AS path_a, fb.path AS path_b,
               ra.name AS repo_a, rb.name AS repo_b,
               m.n_ab, m.n_a, m.n_b, m.n_total, p.n_ab_ticket,
               m.{order} AS score, m.npmi, m.jaccard, m.phi,
               m.log_likelihood_ratio, m.confidence_ab, m.confidence_ba,
               p.last_co_change,
               CASE WHEN m.n_ab > 0
                    THEN p.n_ab_ticket::float / m.n_ab ELSE 0 END AS ticket_ratio
        FROM xrepo_file_pair_metric m
        JOIN xrepo_file_pair p ON p.file_a_id = m.file_a_id AND p.file_b_id = m.file_b_id
        JOIN file fa ON fa.id = m.file_a_id
        JOIN file fb ON fb.id = m.file_b_id
        JOIN repo ra ON ra.id = m.repo_a_id
        JOIN repo rb ON rb.id = m.repo_b_id
        WHERE m.n_ab >= %(min_support)s
        ORDER BY m.{order} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params,
    )


def repo_chains(
    repo_id: int,
    max_depth: int | None = None,
    min_confidence: float | None = None,
    limit: int = 40,
    min_support: int = 3,
) -> list[dict]:
    """Transitive coupling chains outward from one repository.

    Answers the multi-hop form of the question: "changing A means changing B,
    and that means changing C". Implemented as a recursive walk over the
    *directed* view of ``repo_pair_metric``, where the weight of a hop is
    ``P(next | current)``.

    Path confidence is the **product** of the hop confidences, which is the
    correct composition if the hops are treated as conditionally independent.
    That is an approximation -- real dependencies are correlated, so the product
    is a lower bound -- but it has the property that matters: adding a weak hop
    can only weaken the chain, so long speculative paths sink on their own.

    Cycles are prevented by carrying the visited path and refusing to revisit,
    which also stops A->B->A being reported as a discovery.

    Args:
        repo_id: the repository to walk out from.
        max_depth: maximum hops. 2 means A->B->C.
        min_confidence: per-hop floor; hops below it are not followed.
        limit: maximum chains returned.
        min_support: minimum shared change sets for a hop to be considered.

    Returns:
        Chains of length >= 2 hops, strongest path confidence first. Each row
        carries the repo names in order and the per-hop confidences.
    """
    cfg = get_config().crossrepo
    depth = max(1, min(int(max_depth or cfg.chain_max_depth), 5))
    floor = float(cfg.chain_min_confidence if min_confidence is None else min_confidence)

    return query(
        """
        WITH RECURSIVE directed AS (
            -- Both orientations of every pair, as directed edges with the
            -- correct conditional probability for that direction.
            SELECT m.repo_a_id AS src, m.repo_b_id AS dst,
                   m.confidence_ab AS conf, m.n_ab, m.npmi
            FROM repo_pair_metric m WHERE m.n_ab >= %(min_support)s
            UNION ALL
            SELECT m.repo_b_id, m.repo_a_id, m.confidence_ba, m.n_ab, m.npmi
            FROM repo_pair_metric m WHERE m.n_ab >= %(min_support)s
        ),
        walk AS (
            SELECT d.src, d.dst, 1 AS depth,
                   d.conf AS path_conf,
                   ARRAY[d.src, d.dst] AS path,
                   ARRAY[round(d.conf::numeric, 4)] AS hops,
                   ARRAY[d.n_ab] AS supports
            FROM directed d
            WHERE d.src = %(repo_id)s AND d.conf >= %(floor)s

            UNION ALL

            SELECT w.src, d.dst, w.depth + 1,
                   w.path_conf * d.conf,
                   w.path || d.dst,
                   w.hops || round(d.conf::numeric, 4),
                   w.supports || d.n_ab
            FROM walk w
            JOIN directed d ON d.src = w.dst
            WHERE w.depth < %(depth)s
              AND d.conf >= %(floor)s
              AND NOT d.dst = ANY(w.path)     -- no cycles, no revisits
        )
        SELECT w.depth, w.path_conf, w.path, w.hops, w.supports,
               (SELECT array_agg(r.name ORDER BY ord)
                  FROM unnest(w.path) WITH ORDINALITY AS u(id, ord)
                  JOIN repo r ON r.id = u.id) AS repo_names
        FROM walk w
        WHERE w.depth >= 2
        ORDER BY w.path_conf DESC, w.depth
        LIMIT %(limit)s
        """,
        {
            "repo_id": repo_id,
            "depth": depth,
            "floor": floor,
            "limit": _clamp_limit(limit),
            "min_support": max(min_support, 1),
        },
    )


def crossrepo_graph(
    measure: str = DEFAULT_MEASURE,
    limit: int = 200,
    min_support: int = 5,
    center_repo_id: int | None = None,
) -> dict:
    """Repo-level node/edge graph, for the cross-repo force-directed view."""
    order = _safe_order(measure)
    params: dict[str, Any] = {
        "limit": max(1, min(int(limit), 2000)),
        "min_support": max(min_support, 1),
    }
    filters = ["m.n_ab >= %(min_support)s"]
    if center_repo_id:
        filters.append("(m.repo_a_id = %(center)s OR m.repo_b_id = %(center)s)")
        params["center"] = center_repo_id

    edges = query(
        f"""
        SELECT m.repo_a_id AS source, m.repo_b_id AS target,
               m.{order} AS score, m.n_ab, m.npmi, m.log_likelihood_ratio,
               m.confidence_ab, m.confidence_ba, p.n_ab_ticket
        FROM repo_pair_metric m
        JOIN repo_pair p ON p.repo_a_id = m.repo_a_id AND p.repo_b_id = m.repo_b_id
        WHERE {' AND '.join(filters)}
        ORDER BY m.{order} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params,
    )

    ids = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
    nodes: list[dict] = []
    if ids:
        nodes = query(
            """
            SELECT r.id, r.name AS basename, r.full_name AS path,
                   COALESCE(r.primary_language, '') AS dir_path,
                   r.primary_language AS extension,
                   COALESCE(s.change_set_count, r.commit_count) AS change_count,
                   FALSE AS is_deleted, r.commit_count, r.is_private
            FROM repo r
            LEFT JOIN repo_change_stats s ON s.repo_id = r.id
            WHERE r.id = ANY(%(ids)s)
            """,
            {"ids": ids},
        )

    return {
        "measure": order,
        "nodes": nodes,
        "edges": edges,
        "stats": {"node_count": len(nodes), "edge_count": len(edges)},
    }


def change_sets_for_pair(repo_a_id: int, repo_b_id: int, limit: int = 25) -> list[dict]:
    """The change sets in which two repositories both changed -- the evidence."""
    return query(
        """
        SELECT cs.id, cs.key, cs.signal, cs.ticket, cs.n_commits, cs.n_repos,
               cs.n_files, cs.first_at, cs.last_at, a.display_name AS author
        FROM change_set cs
        LEFT JOIN author a ON a.id = cs.author_id
        WHERE cs.pair_eligible
          AND EXISTS (SELECT 1 FROM change_set_commit x
                       WHERE x.change_set_id = cs.id AND x.repo_id = %(a)s)
          AND EXISTS (SELECT 1 FROM change_set_commit y
                       WHERE y.change_set_id = cs.id AND y.repo_id = %(b)s)
        ORDER BY cs.last_at DESC
        LIMIT %(limit)s
        """,
        {"a": repo_a_id, "b": repo_b_id, "limit": _clamp_limit(limit)},
    )


def change_set_detail(change_set_id: int) -> dict | None:
    """One change set with its commits, grouped by repository."""
    row = query_one(
        """
        SELECT cs.*, a.display_name AS author, a.email AS author_email
        FROM change_set cs LEFT JOIN author a ON a.id = cs.author_id
        WHERE cs.id = %s
        """,
        (change_set_id,),
    )
    if row is None:
        return None
    row["commits"] = query(
        """
        SELECT c.id, c.sha, c.subject, c.committed_at, c.n_files,
               c.insertions, c.deletions, r.name AS repo, r.id AS repo_id,
               au.display_name AS author
        FROM change_set_commit csc
        JOIN commit c ON c.id = csc.commit_id
        JOIN repo r ON r.id = csc.repo_id
        LEFT JOIN author au ON au.id = c.author_id
        WHERE csc.change_set_id = %s
        ORDER BY c.committed_at
        """,
        (change_set_id,),
    )
    return row


def recent_change_sets(
    signal: str | None = None, multi_repo_only: bool = True, limit: int = 50
) -> list[dict]:
    """Recent change sets, for browsing the raw cross-repo evidence."""
    clauses = ["cs.pair_eligible"]
    params: dict[str, Any] = {"limit": _clamp_limit(limit)}
    if signal:
        clauses.append("cs.signal = %(signal)s")
        params["signal"] = signal
    if multi_repo_only:
        clauses.append("cs.n_repos > 1")

    return query(
        f"""
        SELECT cs.id, cs.key, cs.signal, cs.ticket, cs.n_commits, cs.n_repos,
               cs.n_files, cs.first_at, cs.last_at, a.display_name AS author,
               (SELECT array_agg(DISTINCT r.name)
                  FROM change_set_commit x JOIN repo r ON r.id = x.repo_id
                 WHERE x.change_set_id = cs.id) AS repos
        FROM change_set cs
        LEFT JOIN author a ON a.id = cs.author_id
        WHERE {' AND '.join(clauses)}
        ORDER BY cs.last_at DESC
        LIMIT %(limit)s
        """,
        params,
    )


# ---------------------------------------------------------------------------
# Intra-repository module graph
# ---------------------------------------------------------------------------


def module_context(repo_id: int, path: str) -> dict:
    """The module owning ``path``, what it declares, and what declares it.

    In a monorepo this is the structural prior that cross-repo analysis cannot
    provide, because every internal reference points back at the same repository.
    The reverse direction usually matters more: changing a shared module is a
    change to everything that declares it, and no co-change score states that as
    plainly as the manifest does.

    Returns an empty ``modules`` list for a single-module repository, which is
    the correct answer rather than an error.
    """
    rows = query(
        """
        SELECT DISTINCT consumer_module, dep_module, manifest
        FROM module_dependency WHERE repo_id = %(repo)s
        """,
        {"repo": repo_id},
    )
    if not rows:
        return {"owning_module": None, "declares": [], "declared_by": [], "modules": []}

    modules = sorted({r["consumer_module"] for r in rows} | {r["dep_module"] for r in rows})

    # The owning module is the longest module path that prefixes this file, so a
    # file under gateway/internal/app belongs to `gateway` rather than the root.
    normalised = path.strip().lstrip("./")
    owning = ""
    for module in modules:
        if not module:
            continue
        if normalised == module or normalised.startswith(module + "/"):
            if len(module) > len(owning):
                owning = module

    return {
        "owning_module": owning or "",
        "declares": sorted(
            {r["dep_module"] for r in rows if r["consumer_module"] == owning}
        ),
        "declared_by": sorted(
            {r["consumer_module"] for r in rows if r["dep_module"] == owning}
        ),
        "modules": modules,
    }


# ---------------------------------------------------------------------------
# Feedback: defects in Git Synapse reported by the sessions that use it
# ---------------------------------------------------------------------------

#: Report kinds accepted. Constrained so the table stays a defect log rather
#: than a comment box.
FEEDBACK_KINDS = (
    "missing_data",   # something that should be indexed is absent
    "wrong_data",     # a value contradicts the repository
    "stale_data",     # correct once, no longer true
    "tool_error",     # a tool failed or returned something unusable
    "coverage_gap",   # a repo, path or ecosystem is not covered
    "suggestion",     # a concrete improvement, not a general opinion
)

FEEDBACK_SEVERITIES = ("low", "medium", "high")


def record_feedback(
    kind: str,
    detail: str,
    severity: str = "medium",
    tool: str | None = None,
    args: dict | None = None,
    repo: str | None = None,
    path: str | None = None,
    expected: str | None = None,
    observed: str | None = None,
) -> dict:
    """Record a defect in Git Synapse, deduplicating on its content.

    Writes only to ``feedback``, which nothing else reads. See the table comment
    in ``schema.sql`` for why that boundary exists.

    A repeat of the same defect increments ``occurrences`` rather than adding a
    row, so the count doubles as a priority signal: a gap twenty sessions hit
    matters more than one seen once.

    Raises:
        ValueError: on an unknown kind or severity, or an empty detail.
    """
    import hashlib
    import json as _json

    from git_synapse.db.engine import connection

    if kind not in FEEDBACK_KINDS:
        raise ValueError(
            f"unknown kind {kind!r}; expected one of {', '.join(FEEDBACK_KINDS)}"
        )
    if severity not in FEEDBACK_SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    if not (detail or "").strip():
        raise ValueError("detail is required: describe the defect concretely")

    # Fingerprint on the identity of the defect, not its prose, so the same gap
    # described in different words still collapses to one row. With no locating
    # context there is no identity to collapse on, so fall back to the prose --
    # otherwise two unrelated reports of the same kind become one and the second
    # is discarded.
    context = (tool or "", repo or "", path or "", (expected or "").strip().lower()[:200])
    seed = "|".join((kind, *context) if any(context) else (kind, detail.strip().lower()[:200]))
    fingerprint = hashlib.sha256(seed.encode()).hexdigest()[:32]

    # Cutting a serialised JSON string mid-token leaves invalid JSON, and the
    # column is jsonb, so an oversized payload has to be replaced rather than
    # trimmed.
    payload = _json.dumps(args or {}, default=str)
    if len(payload) > 4000:
        payload = _json.dumps({"truncated": True, "preview": payload[:3800]})

    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO feedback (kind, severity, tool, args, repo, path,
                                  expected, observed, detail, fingerprint)
            VALUES (%(kind)s, %(severity)s, %(tool)s, %(args)s::jsonb, %(repo)s,
                    %(path)s, %(expected)s, %(observed)s, %(detail)s, %(fp)s)
            ON CONFLICT (fingerprint) DO UPDATE SET
                occurrences  = feedback.occurrences + 1,
                last_seen_at = now(),
                -- A repeat of something already closed is a reopen.
                status = CASE WHEN feedback.status IN ('fixed', 'wontfix')
                              THEN 'open' ELSE feedback.status END,
                resolution = CASE WHEN feedback.status IN ('fixed', 'wontfix')
                                  THEN NULL ELSE feedback.resolution END,
                resolved_at = CASE WHEN feedback.status IN ('fixed', 'wontfix')
                                   THEN NULL ELSE feedback.resolved_at END,
                -- Keep the worse severity. GREATEST() on text would rank
                -- 'low' above 'high' alphabetically, so rank explicitly.
                severity = CASE
                    WHEN 'high'   IN (feedback.severity, EXCLUDED.severity) THEN 'high'
                    WHEN 'medium' IN (feedback.severity, EXCLUDED.severity) THEN 'medium'
                    ELSE 'low'
                END
            RETURNING id, occurrences, status, first_seen_at
            """,
            {
                "kind": kind,
                "severity": severity,
                "tool": tool,
                "args": payload,
                "repo": repo,
                "path": path,
                "expected": (expected or "")[:2000] or None,
                "observed": (observed or "")[:2000] or None,
                "detail": detail[:4000],
                "fp": fingerprint,
            },
        ).fetchone()

    return {
        "id": int(row[0]),
        "occurrences": int(row[1]),
        "status": row[2],
        "first_seen": str(row[3]),
        "deduplicated": int(row[1]) > 1,
    }


def list_feedback(
    status: str | None = "open", kind: str | None = None, limit: int = 50
) -> list[dict]:
    """Reported defects, most-hit first."""
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": _clamp_limit(limit)}
    if status:
        clauses.append("status = %(status)s")
        params["status"] = status
    if kind:
        clauses.append("kind = %(kind)s")
        params["kind"] = kind
    return query(
        f"""
        SELECT * FROM feedback
        WHERE {' AND '.join(clauses)}
        ORDER BY occurrences DESC, last_seen_at DESC
        LIMIT %(limit)s
        """,
        params,
    )


def resolve_feedback(feedback_id: int, status: str, resolution: str) -> bool:
    """Close or reclassify a report. Human action, not an agent's."""
    from git_synapse.db.engine import execute

    if status not in ("open", "investigating", "fixed", "wontfix"):
        raise ValueError(f"unknown status {status!r}")
    return execute(
        """
        UPDATE feedback SET status = %s, resolution = %s,
               resolved_at = CASE WHEN %s IN ('fixed','wontfix') THEN now() END
        WHERE id = %s
        """,
        (status, resolution[:2000], status, feedback_id),
    ) > 0


def feedback_summary() -> dict:
    """Counts for the dashboard."""
    return query_one(
        """
        SELECT
          count(*)                                          AS total,
          count(*) FILTER (WHERE status='open')             AS open,
          count(*) FILTER (WHERE status='fixed')            AS fixed,
          count(*) FILTER (WHERE severity='high'
                             AND status='open')             AS open_high,
          COALESCE(sum(occurrences), 0)                     AS total_hits,
          count(*) FILTER (WHERE last_seen_at > now() - interval '24 hours')
                                                            AS seen_today
        FROM feedback
        """
    ) or {}
