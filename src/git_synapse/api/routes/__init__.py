"""REST endpoints. One router, grouped by resource."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from git_synapse.analysis import mining, predict
from git_synapse.analysis import query as q
from git_synapse.config import get_config
from git_synapse.db.engine import query_one, scalar
from git_synapse.ingest import pipeline
from git_synapse.stats.registry import DEFAULT_MEASURE

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health & meta
# ---------------------------------------------------------------------------


@router.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness probe. Reports database reachability without failing the check."""
    try:
        db_ok = scalar("SELECT 1") == 1
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {"status": "degraded", "database": False, "error": str(exc)}
    return {"status": "ok", "database": db_ok}


@router.get("/overview", tags=["meta"])
def overview() -> dict:
    """Headline counts across the whole corpus."""
    return q.overview()


@router.get("/measures", tags=["meta"])
def measures() -> dict:
    """The full catalogue of association measures, with guidance on each."""
    catalog = q.measure_catalog()
    return {
        "default": DEFAULT_MEASURE,
        "count": len(catalog),
        "measures": catalog,
    }


@router.get("/config", tags=["meta"])
def config() -> dict:
    """Effective tuning parameters, so the UI can explain what it is showing."""
    cfg = get_config()
    return {
        "org": cfg.github.org,
        "max_files_per_commit": cfg.ingest.max_files_per_commit,
        "min_pair_support": cfg.ingest.min_pair_support,
        "include_merges": cfg.ingest.include_merges,
        "rename_similarity": cfg.ingest.rename_similarity,
        "blobless_threshold_kb": cfg.ingest.blobless_threshold_kb,
        "recency_half_life_days": cfg.analysis.recency_half_life_days,
        "crossrepo_enabled": cfg.crossrepo.enabled,
        "session_gap_hours": cfg.crossrepo.session_gap_hours,
        "max_repos_per_changeset": cfg.crossrepo.max_repos_per_changeset,
        "max_files_per_repo_per_changeset": cfg.crossrepo.max_files_per_repo_per_changeset,
        "min_xrepo_support": cfg.crossrepo.min_support,
        "chain_min_confidence": cfg.crossrepo.chain_min_confidence,
        "chain_max_depth": cfg.crossrepo.chain_max_depth,
        "ticket_pattern": cfg.crossrepo.ticket_pattern,
        "refresh_cron": cfg.schedule.cron,
        "scheduler_timezone": cfg.schedule.timezone,
        "scheduler_enabled": cfg.schedule.enabled,
    }


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


@router.get("/repos", tags=["repos"])
def list_repos(
    search: str | None = None,
    language: str | None = None,
    status: str | None = None,
    order_by: str = "commit_count",
    descending: bool = True,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """List repositories with ingest state and history summary."""
    rows = q.list_repos(search, language, status, order_by, descending, limit, offset)
    return {"count": len(rows), "repos": rows}


@router.get("/repos/languages", tags=["repos"])
def languages() -> dict:
    return {"languages": q.repo_languages()}


@router.get("/repos/{repo_id}", tags=["repos"])
def get_repo(repo_id: int) -> dict:
    repo = q.get_repo(repo_id)
    if repo is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    return repo


@router.get("/repos/{repo_id}/files", tags=["repos"])
def repo_files(
    repo_id: int,
    search: str | None = None,
    extension: str | None = None,
    min_changes: int = 0,
    order_by: str = "change_count",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    rows = q.search_files(search, repo_id, extension, min_changes, order_by, limit, offset)
    return {"count": len(rows), "files": rows}


@router.get("/repos/{repo_id}/directories", tags=["repos"])
def repo_directories(repo_id: int, limit: int = Query(200, ge=1, le=1000)) -> dict:
    return {"directories": q.directories(repo_id, limit)}


@router.get("/repos/{repo_id}/extensions", tags=["repos"])
def repo_extensions(repo_id: int) -> dict:
    return {"extensions": q.file_extensions(repo_id)}


@router.get("/repos/{repo_id}/hotspots", tags=["repos"])
def repo_hotspots(repo_id: int, limit: int = Query(25, ge=1, le=200)) -> dict:
    return {"hotspots": q.hotspots(repo_id, limit)}


@router.get("/repos/{repo_id}/pairs", tags=["coupling"])
def repo_pairs(
    repo_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(50, ge=1, le=1000),
    min_support: int = Query(3, ge=1),
) -> dict:
    """Strongest couplings inside one repository."""
    return {
        "measure": measure,
        "pairs": q.strongest_pairs(repo_id, measure, limit, min_support),
    }


@router.get("/repos/{repo_id}/graph", tags=["coupling"])
def repo_graph(
    repo_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(150, ge=1, le=2000),
    min_support: int = Query(2, ge=1),
    center_file_id: int | None = None,
    min_score: float | None = None,
) -> dict:
    """Node/edge graph of the strongest couplings, for the force-directed view."""
    return q.coupling_graph(repo_id, measure, limit, min_support, center_file_id, min_score)


# ---------------------------------------------------------------------------
# Files & coupling
# ---------------------------------------------------------------------------


@router.get("/files", tags=["files"])
def search_files(
    search: str | None = None,
    repo_id: int | None = None,
    extension: str | None = None,
    min_changes: int = 0,
    order_by: str = "change_count",
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    rows = q.search_files(search, repo_id, extension, min_changes, order_by, limit, offset)
    return {"count": len(rows), "files": rows}


@router.get("/files/resolve", tags=["files"])
def resolve_file(repo: str, path: str) -> dict:
    """Look a file up by repo and path, following renames through the alias table."""
    row = q.resolve_file(repo, path)
    if row is None:
        raise HTTPException(404, f"no file {path!r} in repository {repo!r}")
    return row


@router.get("/files/{file_id}", tags=["files"])
def get_file(file_id: int) -> dict:
    row = q.get_file(file_id)
    if row is None:
        raise HTTPException(404, f"file {file_id} not found")
    return row


@router.get("/files/{file_id}/coupled", tags=["coupling"])
def coupled(
    file_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(25, ge=1, le=1000),
    min_support: int = Query(1, ge=1),
    min_score: float | None = None,
) -> dict:
    """Files that historically change together with this one, ranked.

    The central question of the product: "I am editing this, what else must
    change?"
    """
    if q.get_file(file_id) is None:
        raise HTTPException(404, f"file {file_id} not found")
    return {
        "file_id": file_id,
        "measure": measure,
        "partners": q.coupled_files(file_id, measure, limit, min_support, min_score),
    }


@router.get("/files/{file_id}/commits", tags=["files"])
def file_commits(file_id: int, limit: int = Query(50, ge=1, le=500)) -> dict:
    return {"commits": q.file_commits(file_id, limit)}


@router.get("/files/{file_id}/authors", tags=["files"])
def file_authors(file_id: int, limit: int = Query(20, ge=1, le=200)) -> dict:
    return {"authors": q.file_authors(file_id, limit)}


@router.get("/pairs/{file_a_id}/{file_b_id}", tags=["coupling"])
def pair_detail(file_a_id: int, file_b_id: int) -> dict:
    """Full contingency table and every measure for one pair."""
    row = q.pair_detail(file_a_id, file_b_id)
    if row is None:
        raise HTTPException(404, "no recorded coupling between those files")
    return row


@router.get("/pairs/{file_a_id}/{file_b_id}/commits", tags=["coupling"])
def pair_commits(
    file_a_id: int, file_b_id: int, limit: int = Query(25, ge=1, le=200)
) -> dict:
    """The commits where both files changed -- the evidence behind the score."""
    return {"commits": q.co_change_commits(file_a_id, file_b_id, limit)}


@router.get("/directories/{dir_id}/coupled", tags=["coupling"])
def coupled_dirs(
    dir_id: int, measure: str = DEFAULT_MEASURE, limit: int = Query(25, ge=1, le=500)
) -> dict:
    return {"measure": measure, "partners": q.coupled_directories(dir_id, measure, limit)}


@router.get("/pairs", tags=["coupling"])
def top_pairs(
    measure: str = DEFAULT_MEASURE,
    repo_id: int | None = None,
    limit: int = Query(50, ge=1, le=1000),
    min_support: int = Query(3, ge=1),
) -> dict:
    """Strongest couplings, optionally across the whole org."""
    return {
        "measure": measure,
        "pairs": q.strongest_pairs(repo_id, measure, limit, min_support),
    }


@router.get("/hotspots", tags=["files"])
def hotspots(repo_id: int | None = None, limit: int = Query(25, ge=1, le=200)) -> dict:
    return {"hotspots": q.hotspots(repo_id, limit)}


# ---------------------------------------------------------------------------
# Cross-repository coupling
# ---------------------------------------------------------------------------


@router.get("/crossrepo/overview", tags=["crossrepo"])
def crossrepo_overview() -> dict:
    """Change-set counts and cross-repo pair counts."""
    return q.crossrepo_overview()


@router.get("/crossrepo/pairs", tags=["crossrepo"])
def crossrepo_pairs(
    measure: str = DEFAULT_MEASURE,
    level: str = Query("repo", pattern="^(repo|file)$"),
    limit: int = Query(50, ge=1, le=1000),
    min_support: int = Query(5, ge=1),
) -> dict:
    """Strongest cross-repository couplings, at repo or file granularity."""
    return {
        "measure": measure,
        "level": level,
        "pairs": q.top_crossrepo_pairs(measure, limit, min_support, level),
    }


@router.get("/crossrepo/graph", tags=["crossrepo"])
def crossrepo_graph(
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(200, ge=1, le=2000),
    min_support: int = Query(5, ge=1),
    center_repo_id: int | None = None,
) -> dict:
    """Repo-level node/edge graph of cross-repository coupling."""
    return q.crossrepo_graph(measure, limit, min_support, center_repo_id)


@router.get("/repos/{repo_id}/partners", tags=["crossrepo"])
def repo_partners(
    repo_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(25, ge=1, le=500),
    min_support: int = Query(2, ge=1),
) -> dict:
    """Other repositories that change together with this one."""
    if q.get_repo(repo_id) is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    return {
        "repo_id": repo_id,
        "measure": measure,
        "partners": q.repo_partners(repo_id, measure, limit, min_support),
    }


@router.get("/repos/{repo_id}/chains", tags=["crossrepo"])
def repo_chains(
    repo_id: int,
    max_depth: int = Query(3, ge=1, le=5),
    min_confidence: float = Query(0.15, ge=0.0, le=1.0),
    min_support: int = Query(3, ge=1),
    limit: int = Query(40, ge=1, le=200),
) -> dict:
    """Transitive coupling chains: changing this repo implies B implies C.

    Path confidence is the product of the per-hop conditional probabilities, so
    a weak hop can only weaken a chain.
    """
    if q.get_repo(repo_id) is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    chains = q.repo_chains(repo_id, max_depth, min_confidence, limit, min_support)
    return {
        "repo_id": repo_id,
        "max_depth": max_depth,
        "min_confidence": min_confidence,
        "count": len(chains),
        "chains": [
            {
                "depth": c["depth"],
                "path_confidence": float(c["path_conf"]),
                "repo_ids": list(c["path"]),
                "repos": list(c["repo_names"] or []),
                "hop_confidences": [float(h) for h in c["hops"]],
                "hop_supports": list(c["supports"]),
            }
            for c in chains
        ],
    }


@router.get("/repos/{repo_a_id}/partners/{repo_b_id}/change-sets", tags=["crossrepo"])
def pair_change_sets(
    repo_a_id: int, repo_b_id: int, limit: int = Query(25, ge=1, le=200)
) -> dict:
    """The change sets in which both repositories changed -- the evidence."""
    return {"change_sets": q.change_sets_for_pair(repo_a_id, repo_b_id, limit)}


@router.get("/files/{file_id}/coupled-crossrepo", tags=["crossrepo"])
def file_crossrepo(
    file_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(25, ge=1, le=500),
    min_support: int = Query(2, ge=1),
) -> dict:
    """Files in *other* repositories that change together with this file."""
    if q.get_file(file_id) is None:
        raise HTTPException(404, f"file {file_id} not found")
    return {
        "file_id": file_id,
        "measure": measure,
        "partners": q.crossrepo_file_partners(file_id, measure, limit, min_support),
    }


@router.get("/change-sets", tags=["crossrepo"])
def change_sets(
    signal: str | None = Query(None, pattern="^(ticket|temporal)$"),
    multi_repo_only: bool = True,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """Recent change sets -- the raw units cross-repo coupling is computed over."""
    return {"change_sets": q.recent_change_sets(signal, multi_repo_only, limit)}


@router.get("/change-sets/{change_set_id}", tags=["crossrepo"])
def change_set_detail(change_set_id: int) -> dict:
    """One change set with all its commits, grouped by repository."""
    row = q.change_set_detail(change_set_id)
    if row is None:
        raise HTTPException(404, f"change set {change_set_id} not found")
    return row


# ---------------------------------------------------------------------------
# Impact prediction
# ---------------------------------------------------------------------------


@router.get("/repos/{repo_id}/impact", tags=["impact"])
def repo_impact(
    repo_id: int,
    direction: str = Query("downstream", pattern="^(downstream|upstream)$"),
    limit: int = Query(20, ge=1, le=200),
    declared_only: bool = False,
) -> dict:
    """What else to look at when changing this repository.

    ``downstream`` is what a change here forces others to update; ``upstream`` is
    where a change here may actually belong.
    """
    if q.get_repo(repo_id) is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    rows = (
        predict.upstream_of(repo_id, limit=limit)
        if direction == "upstream"
        else predict.impact_for(repo_id, limit=limit, declared_only=declared_only)
    )
    return {"repo_id": repo_id, "direction": direction, "edges": rows}


@router.get("/repos/{repo_id}/impact-chains", tags=["impact"])
def repo_impact_chains(
    repo_id: int,
    direction: str = Query("downstream", pattern="^(downstream|upstream)$"),
    max_depth: int = Query(3, ge=1, le=5),
    min_score: float = Query(0.3, ge=0.0, le=1.0),
    limit: int = Query(40, ge=1, le=200),
) -> dict:
    """Transitive impact chains over validated edges only."""
    if q.get_repo(repo_id) is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    fn = predict.upstream_chains if direction == "upstream" else predict.impact_chains
    rows = fn(repo_id, max_depth=max_depth, min_score=min_score, limit=limit)
    return {
        "repo_id": repo_id,
        "direction": direction,
        "chains": [
            {
                "depth": c["depth"],
                "path_score": float(c["path_score"]),
                "repos": list(c["repo_names"] or []),
                "repo_ids": list(c["path"]),
                "hops": [float(h) for h in (c["hops"] or [])],
                "lags": [float(x) if x is not None else None for x in (c.get("lags") or [])],
            }
            for c in rows
        ],
    }


@router.get("/impact/graph", tags=["impact"])
def impact_graph(
    min_score: float = Query(0.4, ge=0.0, le=1.0),
    validated_only: bool = True,
    limit: int = Query(400, ge=1, le=3000),
) -> dict:
    """Repository-level impact graph, for the force-directed view."""
    from git_synapse.db.engine import query as raw

    clauses = ["i.score >= %(min_score)s"]
    if validated_only:
        clauses.append("(i.is_declared OR i.has_bump_history)")
    edges = raw(
        f"""
        SELECT i.source_repo_id AS source, i.target_repo_id AS target,
               i.score, i.is_declared, i.has_bump_history, i.bump_count,
               i.median_lag_days
        FROM repo_impact i
        WHERE {' AND '.join(clauses)}
        ORDER BY i.score DESC
        LIMIT %(limit)s
        """,
        {"min_score": min_score, "limit": limit},
    )
    ids = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
    nodes = raw(
        """
        SELECT r.id, r.name AS basename, r.full_name AS path,
               COALESCE(r.primary_language,'') AS dir_path,
               r.primary_language AS extension, r.commit_count AS change_count,
               FALSE AS is_deleted
        FROM repo r WHERE r.id = ANY(%(ids)s)
        """,
        {"ids": ids},
    ) if ids else []
    return {"nodes": nodes, "edges": edges,
            "stats": {"node_count": len(nodes), "edge_count": len(edges)}}


@router.get("/repos/{repo_id}/dependencies", tags=["impact"])
def repo_dependencies(repo_id: int) -> dict:
    """Declared dependencies and observed bumps for one repository."""
    from git_synapse.db.engine import query as raw

    declared = raw(
        """
        SELECT d.dep_name, d.dep_version, d.manifest, d.ecosystem, d.dep_repo_id,
               r.name AS dep_repo
        FROM repo_dependency d
        LEFT JOIN repo r ON r.id = d.dep_repo_id
        WHERE d.consumer_repo_id = %(repo)s
        ORDER BY (d.dep_repo_id IS NULL), d.dep_name
        """,
        {"repo": repo_id},
    )
    bumps = raw(
        """
        SELECT rd.name AS dep_repo, b.dep_repo_id, count(*) AS bumps,
               round((percentile_cont(0.5) WITHIN GROUP (ORDER BY b.lag_seconds)
                      / 86400.0)::numeric, 2) AS median_lag_days,
               max(b.bumped_at) AS last_bump
        FROM dep_bump b
        LEFT JOIN repo rd ON rd.id = b.dep_repo_id
        WHERE b.consumer_repo_id = %(repo)s AND b.dep_repo_id IS NOT NULL
        GROUP BY 1, 2 ORDER BY bumps DESC
        """,
        {"repo": repo_id},
    )
    return {"declared": declared, "bumps": bumps}


# ---------------------------------------------------------------------------
# Directional / lagged analysis
# ---------------------------------------------------------------------------


@router.get("/repos/{repo_a_id}/lag-profile/{repo_b_id}", tags=["impact"])
def lag_profile(repo_a_id: int, repo_b_id: int, measure: str = "confidence_ab") -> dict:
    """Association versus lag, in both directions.

    The shape of these two curves is the directional evidence: if A precedes B,
    the forward curve peaks at a positive lag and sits above the reverse one.
    """
    from git_synapse.db.engine import query as raw
    from git_synapse.stats.registry import resolve

    key = resolve(measure).key
    rows = raw(
        f"""
        SELECT lag_bins, bin_hours, repo_a_id, repo_b_id, n_ab, {key} AS value
        FROM repo_lag_metric
        WHERE (repo_a_id = %(a)s AND repo_b_id = %(b)s)
           OR (repo_a_id = %(b)s AND repo_b_id = %(a)s)
        ORDER BY lag_bins
        """,
        {"a": repo_a_id, "b": repo_b_id},
    )
    forward = [r for r in rows if r["repo_a_id"] == repo_a_id]
    reverse = [r for r in rows if r["repo_a_id"] == repo_b_id]
    return {"measure": key, "forward": forward, "reverse": reverse}


# ---------------------------------------------------------------------------
# Mining: modules, drift, risk
# ---------------------------------------------------------------------------


@router.get("/repos/{repo_id}/modules", tags=["mining"])
def repo_modules(repo_id: int, limit: int = Query(20, ge=1, le=200)) -> dict:
    """De-facto modules that cut across the declared directory structure."""
    return {"modules": mining.cross_directory_modules(repo_id, limit)}


@router.get("/drift", tags=["mining"])
def drift(
    trend: str = Query("emerging", pattern="^(emerging|decaying|stable)$"),
    repo_id: int | None = None,
    limit: int = Query(30, ge=1, le=300),
) -> dict:
    """Pairs whose coupling is strengthening or decaying over time."""
    return {"trend": trend, "pairs": mining.drifting_pairs(repo_id, trend, limit)}


@router.get("/risk", tags=["mining"])
def risk(repo_id: int | None = None, limit: int = Query(30, ge=1, le=300)) -> dict:
    """Files where churn, coupling and concentrated ownership coincide."""
    return {"files": mining.risky_files(repo_id, limit)}


@router.get("/mining/overview", tags=["mining"])
def mining_overview() -> dict:
    """Counts for the mining layer."""
    from git_synapse.db.engine import query_one as one

    return one(
        """
        SELECT
          -- cluster_id restarts at 0 in every repository, so a module is
          -- identified by the (repo_id, cluster_id) pair. Counting cluster_id
          -- alone collapsed 4,394 modules down to 559.
          (SELECT count(*) FROM (SELECT DISTINCT repo_id, cluster_id
                                   FROM file_cluster) m)                 AS modules,
          (SELECT count(*) FROM file_cluster)                            AS clustered_files,
          (SELECT count(*) FROM (SELECT DISTINCT repo_id, cluster_id
                                   FROM file_cluster
                                  WHERE dirs_spanned > 1) m)             AS cross_dir_modules,
          (SELECT count(*) FROM pair_drift WHERE trend='emerging')       AS emerging,
          (SELECT count(*) FROM pair_drift WHERE trend='decaying')       AS decaying,
          (SELECT count(*) FROM pair_drift WHERE trend='stable')         AS stable,
          (SELECT count(*) FROM file_risk)                               AS risk_scored,
          (SELECT count(*) FROM repo_impact)                             AS impact_edges,
          (SELECT count(*) FROM repo_impact WHERE is_declared)           AS declared_edges,
          (SELECT count(*) FROM repo_impact WHERE has_bump_history)      AS bump_edges,
          (SELECT count(*) FROM dep_bump)                                AS dep_bumps,
          (SELECT count(*) FROM repo_dependency
            WHERE dep_repo_id IS NOT NULL)                               AS declared_deps,
          (SELECT count(*) FROM repo_lag_metric)                         AS lagged_rows
        """
    ) or {}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@router.get("/validation", tags=["mining"])
def validation(
    lag_bins: int = Query(1, ge=0, le=100),
    min_bumps: int = Query(2, ge=1),
    limit: int = Query(35, ge=1, le=40),
) -> dict:
    """How well each measure predicts real dependency propagation.

    Ranks every measure by AUC against the manifest-bump ground truth. This is
    the page that says which numbers to trust, and it is computed rather than
    asserted.
    """
    from git_synapse.analysis.validate import evaluate

    scored = evaluate(lag_bins=lag_bins, min_bumps=min_bumps)[:limit]
    return {
        "lag_bins": lag_bins,
        "min_bumps": min_bumps,
        "candidates": scored[0].n_candidates if scored else 0,
        "true_edges": scored[0].n_true if scored else 0,
        "note": (
            "AUC here is measured over ALL ordered repository pairs. Note that a "
            "high AUC is not the same as a useful answer: russell_rao tops this "
            "table while managing only ~0.63 directional accuracy, because it is "
            "pure joint frequency and mostly ranks 'both repos are busy'. "
            "Restricting "
            "candidates to declared dependencies raises it to ~0.93, "
            "which is why the impact view ranks within that structural set."
        ),
        "measures": [
            {
                "measure": s.measure,
                "auc": None if s.auc != s.auc else round(s.auc, 4),
                "precision_at": {str(k): round(v, 3) for k, v in s.precision_at.items()},
                "directional_accuracy": (
                    round(s.directional_accuracy, 4)
                    if s.directional_accuracy is not None else None
                ),
            }
            for s in scored
        ],
    }


# ---------------------------------------------------------------------------
# Feedback: defects in Git Synapse reported by the sessions using it
# ---------------------------------------------------------------------------


@router.get("/feedback", tags=["feedback"])
def feedback(
    status: str | None = "open",
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """Defects reported against Git Synapse, most-hit first."""
    return {
        "summary": q.feedback_summary(),
        "reports": q.list_feedback(status, kind, limit),
    }


@router.post("/feedback/{feedback_id}/resolve", tags=["feedback"])
def resolve_feedback(feedback_id: int, status: str, resolution: str = "") -> dict:
    """Close or reclassify a report. A human action, not an agent's."""
    try:
        ok = q.resolve_feedback(feedback_id, status, resolution)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, f"report {feedback_id} not found")
    return {"id": feedback_id, "status": status}


# ---------------------------------------------------------------------------
# Ingest control
# ---------------------------------------------------------------------------


@router.get("/runs", tags=["ingest"])
def runs(limit: int = Query(20, ge=1, le=200)) -> dict:
    return {"runs": q.recent_runs(limit)}


@router.get("/runs/{run_id}", tags=["ingest"])
def run_detail(run_id: int) -> dict:
    row = q.run_detail(run_id)
    if row is None:
        raise HTTPException(404, f"run {run_id} not found")
    return row


@router.post("/ingest/refresh", tags=["ingest"])
def trigger_refresh(
    background: BackgroundTasks,
    force_full: bool = False,
    skip_discovery: bool = False,
) -> dict:
    """Kick off an ingest run in the background.

    Returns immediately; poll ``/api/runs`` for progress. A full ingest of a
    large org takes tens of minutes, far longer than any sane HTTP timeout.
    """
    # Reconciles abandoned runs first, so a killed container cannot block
    # ingestion forever.
    running = pipeline.active_run()
    if running:
        raise HTTPException(
            409,
            f"run {running['id']} ({running['trigger']}) is already in progress,"
            f" started {running['started_at']:%Y-%m-%d %H:%M UTC}",
        )

    def _job() -> None:
        records = pipeline.load_repo_records() if skip_discovery else None
        pipeline.run_ingest(records=records, trigger="api", force_full=force_full)

    background.add_task(_job)
    return {"status": "started", "force_full": force_full, "skip_discovery": skip_discovery}
