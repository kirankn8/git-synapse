"""Evidence-backed repository impact prediction using the ORM."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from git_synapse.db.engine import connection, get_watermark, set_watermark
from git_synapse.db.orm import models

log = logging.getLogger(__name__)


@dataclass
class PredictStats:
    sources: int = 0
    rows_written: int = 0
    declared_edges: int = 0
    bumped_edges: int = 0
    duration_s: float = 0.0


def _as_dict(row: object) -> dict:
    return {attr.key: getattr(row, attr.key) for attr in row.__mapper__.column_attrs}


def _input_fingerprint(conn: object) -> str:
    Dependency, Bump = models().RepoDependency, models().DepBump
    # The caller may have appended dependency/bump rows in the same
    # autoflush-disabled transaction. Fingerprinting must include those writes
    # before deciding whether a rebuild can be skipped.
    conn.flush()
    dependencies = conn.query(Dependency).order_by(
        Dependency.consumer_repo_id, Dependency.dep_name, Dependency.manifest,
    ).all()
    bumps = conn.query(Bump).order_by(Bump.id).all()
    parts = ["|".join(str(x) for x in (r.consumer_repo_id, r.dep_repo_id or "", r.dep_name,
                                          r.manifest, r.ecosystem, r.dep_version or "")) for r in dependencies]
    parts += ["|".join(str(x) for x in (r.consumer_repo_id, r.dep_repo_id or "", r.dep_name,
                                          r.dep_version, r.bumped_at, r.adoption_seconds,
                                          r.version_key or "")) for r in bumps]
    return hashlib.md5("\n".join(parts).encode(), usedforsecurity=False).hexdigest()


def rebuild(conn: object | None = None, force: bool = False) -> PredictStats:
    """Recompute the repository impact graph from declared and bump facts."""
    def run(session: object) -> PredictStats:
        started = time.monotonic()
        stats = PredictStats()
        fingerprint = _input_fingerprint(session)
        if not force and get_watermark("predict_inputs") == fingerprint:
            return stats
        Dependency, Bump, Impact = models().RepoDependency, models().DepBump, models().RepoImpact
        dependencies = session.query(Dependency).filter(Dependency.dep_repo_id.is_not(None)).all()
        bumps = session.query(Bump).filter(Bump.dep_repo_id.is_not(None)).all()
        grouped: dict[tuple[int, int], list[object]] = defaultdict(list)
        for bump in bumps:
            if bump.dep_repo_id != bump.consumer_repo_id:
                grouped[(bump.dep_repo_id, bump.consumer_repo_id)].append(bump)
        declared = {(row.dep_repo_id, row.consumer_repo_id) for row in dependencies
                    if row.dep_repo_id != row.consumer_repo_id}
        edges = declared | set(grouped)
        session.query(Impact).delete(synchronize_session=False)
        now = datetime.now(UTC)
        output = []
        rank_groups: dict[int, list[dict]] = defaultdict(list)
        for source, target in edges:
            history = grouped.get((source, target), [])
            last_bump = max((x.bumped_at for x in history if x.bumped_at), default=None)
            lags = sorted(x.adoption_seconds for x in history if x.adoption_seconds is not None)
            median = lags[len(lags) // 2] / 86400.0 if lags else None
            is_declared = (source, target) in declared
            bump_count = len(history)
            recency = 0.2 if last_bump and last_bump > now - timedelta(days=90) else (
                0.1 if last_bump and last_bump > now - timedelta(days=365) else 0.0)
            score = min(1.0, (0.5 if is_declared else 0.2) + min(0.3, bump_count * 0.02) + recency)
            row = {"source_repo_id": source, "target_repo_id": target, "score": score,
                   "is_declared": is_declared, "has_bump_history": bump_count > 0,
                   "bump_count": bump_count, "median_adoption_days": median,
                   "features": {"scored_by": "declared", "bump_count": bump_count,
                                "last_bump": str(last_bump) if last_bump else None}}
            rank_groups[source].append(row)
        for _source, rows in rank_groups.items():
            rows.sort(key=lambda x: x["score"], reverse=True)
            for rank, row in enumerate(rows, 1):
                row["rank_in_source"] = rank
                output.append(Impact(**row))
        session.add_all(output)
        session.flush()
        stats.rows_written = len(output)
        stats.sources = len(rank_groups)
        stats.declared_edges = sum(x.is_declared for x in output)
        stats.bumped_edges = sum(x.has_bump_history for x in output)
        set_watermark("predict_inputs", fingerprint, session)
        stats.duration_s = time.monotonic() - started
        return stats

    if conn is not None:
        return run(conn)
    with connection() as session:
        return run(session)


def _impact_rows(repo_id: int, *, upstream: bool = False, limit: int = 20,
                 declared_only: bool = False, min_score: float = 0.0) -> list[dict]:
    Impact, Repo = models().RepoImpact, models().Repo
    side = Impact.target_repo_id if upstream else Impact.source_repo_id
    other = Impact.source_repo_id if upstream else Impact.target_repo_id
    with connection() as session:
        impacts = session.query(Impact).filter(side == repo_id, Impact.score >= min_score).order_by(
            Impact.is_declared.desc(), Impact.score.desc()).limit(max(limit, 1)).all()
        repos = {r.id: r for r in session.query(Repo).filter(
            Repo.id.in_([getattr(x, other.key) for x in impacts])
        ).all()}
        output = []
        for impact in impacts:
            if declared_only and not impact.is_declared:
                continue
            row = _as_dict(impact)
            target = repos.get(getattr(impact, other.key))
            if target:
                row.update(name=target.name, full_name=target.full_name,
                           primary_language=target.primary_language, description=target.description,
                           commit_count=target.commit_count)
            output.append(row)
        return output


def impact_for(repo_id: int, limit: int = 20, declared_only: bool = False, min_score: float = 0.0) -> list[dict]:
    return _impact_rows(repo_id, limit=limit, declared_only=declared_only, min_score=min_score)


def upstream_of(repo_id: int, limit: int = 20) -> list[dict]:
    return _impact_rows(repo_id, upstream=True, limit=limit)


def _chains(repo_id: int, reverse: bool, max_depth: int, min_score: float, limit: int) -> list[dict]:
    Impact, Repo = models().RepoImpact, models().Repo
    with connection() as session:
        rows = session.query(Impact).filter(Impact.score >= min_score).order_by(Impact.score.desc()).all()
        names = {r.id: r.name for r in session.query(Repo).all()}
    adjacency: dict[int, list[object]] = defaultdict(list)
    for row in rows:
        if not reverse or row.is_declared or row.has_bump_history:
            adjacency[row.target_repo_id if reverse else row.source_repo_id].append(row)
    results = []

    def walk(path: list[int], score: float, hops: list[float], current: int) -> None:
        if len(path) - 1 >= max_depth:
            return
        for edge in adjacency.get(current, []):
            nxt = edge.source_repo_id if reverse else edge.target_repo_id
            if nxt in path:
                continue
            new_path, new_score = [*path, nxt], score * edge.score
            new_hops = [*hops, round(edge.score, 4)]
            if len(new_path) >= 3:
                results.append({"depth": len(new_path) - 1, "path_score": new_score,
                                "path": new_path, "hops": new_hops,
                                "declared": [], "lags": [],
                                "repo_names": [names.get(x, str(x)) for x in new_path]})
            walk(new_path, new_score, new_hops, nxt)

    walk([repo_id], 1.0, [], repo_id)
    results.sort(key=lambda x: x["path_score"], reverse=True)
    return results[:max(limit, 1)]


def impact_chains(repo_id: int, max_depth: int = 3, min_score: float = 0.5, limit: int = 40) -> list[dict]:
    return _chains(repo_id, False, max(1, min(max_depth, 5)), min_score, limit)


def upstream_chains(repo_id: int, max_depth: int = 3, min_score: float = 0.5, limit: int = 25) -> list[dict]:
    return _chains(repo_id, True, max(1, min(max_depth, 5)), min_score, limit)
