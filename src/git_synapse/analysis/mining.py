"""ORM-backed architectural and maintenance-risk analyses."""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)
LABEL_PROPAGATION_ROUNDS = 10
CLUSTER_MIN_SUPPORT = 3
DRIFT_WINDOW_DAYS = 365
RISK_EVIDENCE_K = 25


@dataclass
class MiningStats:
    clusters: int = 0
    clustered_files: int = 0
    cross_directory_clusters: int = 0
    drift_rows: int = 0
    emerging: int = 0
    decaying: int = 0
    risk_rows: int = 0
    duration_s: float = 0.0


def _dict(row: object) -> dict:
    return {attr.key: getattr(row, attr.key) for attr in row.__mapper__.column_attrs}


def _npmi(joint: int, left: int, right: int, population: int) -> float | None:
    if joint <= 0 or left <= 0 or right <= 0 or population <= 0:
        return None
    if joint >= population:
        return 1.0
    value = math.log2((joint * population) / (left * right))
    denominator = -math.log2(joint / population)
    return value / denominator if denominator else 1.0


def rebuild(repo_id: int | None = None, conn: object | None = None, force: bool = False) -> MiningStats:
    def run(session: object) -> MiningStats:
        started = time.monotonic()
        Repo = models().Repo
        if repo_id is not None:
            targets = [repo_id]
        else:
            repos = session.query(Repo).filter(Repo.is_enabled.is_(True), Repo.pair_count > 0).order_by(Repo.id).all()
            targets = [r.id for r in repos if force or r.last_mining_at is None or r.last_aggregate_at is None or r.last_mining_at < r.last_aggregate_at]
        stats = MiningStats()
        for target in targets:
            _cluster_repo(session, target, stats)
            _rebuild_drift(session, target)
            _rebuild_risk(session, target)
            row = session.get(Repo, target)
            if row:
                row.last_mining_at = datetime.now(UTC)
        _refresh_mining_counts(session, stats)
        stats.duration_s = time.monotonic() - started
        return stats

    if conn is not None:
        return run(conn)
    with session_scope() as session:
        return run(session)


def _cluster_repo(session: object, repo_id: int, stats: MiningStats) -> None:
    Pair, Metric, File, Cluster = models().FilePair, models().FilePairMetric, models().File, models().FileCluster
    pairs = session.query(Pair.file_a_id, Pair.file_b_id, Metric.npmi).join(
        Metric, (Metric.repo_id == Pair.repo_id) & (Metric.file_a_id == Pair.file_a_id) & (Metric.file_b_id == Pair.file_b_id)
    ).filter(Pair.repo_id == repo_id, Pair.n_ab >= CLUSTER_MIN_SUPPORT, Metric.npmi > 0).all()
    session.query(Cluster).filter_by(repo_id=repo_id).delete(synchronize_session=False)
    if not pairs:
        return
    nodes = sorted({p.file_a_id for p in pairs} | {p.file_b_id for p in pairs})
    index = {node: i for i, node in enumerate(nodes)}
    src = np.array([index[p.file_a_id] for p in pairs], dtype=np.int64)
    dst = np.array([index[p.file_b_id] for p in pairs], dtype=np.int64)
    weight = np.array([float(p.npmi or 0) for p in pairs], dtype=np.float64)
    labels = np.arange(len(nodes), dtype=np.int64)
    end, other, both = np.concatenate([dst, src]), np.concatenate([src, dst]), np.concatenate([weight, weight])
    for _ in range(LABEL_PROPAGATION_ROUNDS):
        lab = labels[other]
        order = np.lexsort((lab, end))
        end_s, lab_s, w_s = end[order], lab[order], both[order]
        starts = np.r_[True, (end_s[1:] != end_s[:-1]) | (lab_s[1:] != lab_s[:-1])]
        group = np.cumsum(starts) - 1
        totals = np.bincount(group, weights=w_s)
        g_node, g_label = end_s[starts], lab_s[starts]
        pick = np.lexsort((g_label, -totals, g_node))
        pn, pl = g_node[pick], g_label[pick]
        first = np.r_[True, pn[1:] != pn[:-1]]
        new = labels.copy()
        new[pn[first]] = pl[first]
        if np.array_equal(new, labels):
            break
        labels = new
    _, compact = np.unique(labels, return_inverse=True)
    sizes = np.bincount(compact)
    files = {f.id: f for f in session.query(File).filter(File.id.in_(nodes)).all()}
    dirs = {node: (files[node].dir_path or "").split("/")[0] for node in nodes}
    cluster_dirs = defaultdict(set)
    for i, node in enumerate(nodes):
        cluster_dirs[int(compact[i])].add(dirs.get(node, ""))
    inside = np.zeros(len(nodes))
    total = np.zeros(len(nodes))
    same = compact[src] == compact[dst]
    np.add.at(total, src, weight)
    np.add.at(total, dst, weight)
    np.add.at(inside, src[same], weight[same])
    np.add.at(inside, dst[same], weight[same])
    cohesion = np.divide(inside, total, out=np.zeros_like(inside), where=total > 0)
    rows = []
    for i, node in enumerate(nodes):
        cluster_id = int(compact[i])
        if sizes[cluster_id] >= 2:
            rows.append(Cluster(repo_id=repo_id, file_id=node, cluster_id=cluster_id,
                                cluster_size=int(sizes[cluster_id]), cohesion=float(cohesion[i]),
                                dirs_spanned=len(cluster_dirs[cluster_id])))
    session.add_all(rows)
    stats.clustered_files += len(rows)
    stats.clusters += len({x.cluster_id for x in rows})
    stats.cross_directory_clusters += len({x.cluster_id for x in rows if x.dirs_spanned > 1})


def _rebuild_drift(session: object, repo_id: int) -> None:
    Commit, Change, Drift = models().Commit, models().CommitFile, models().PairDrift
    session.query(Drift).filter_by(repo_id=repo_id).delete(synchronize_session=False)
    commits = session.query(Commit).filter_by(repo_id=repo_id, pair_eligible=True).all()
    by_id = {c.id: c for c in commits}
    boundary = datetime.now(UTC) - timedelta(days=DRIFT_WINDOW_DAYS)
    recent = {c.id: c.committed_at >= boundary for c in commits}
    changes = session.query(Change).filter_by(repo_id=repo_id).all()
    by_commit = defaultdict(set)
    for change in changes:
        if change.commit_id in by_id:
            by_commit[change.commit_id].add(change.file_id)
    marg = {True: defaultdict(int), False: defaultdict(int)}
    joint = {True: defaultdict(int), False: defaultdict(int)}
    pop = {True: 0, False: 0}
    for cid, files in by_commit.items():
        window = recent[cid]
        pop[window] += 1
        for file_id in files:
            marg[window][file_id] += 1
        for a, b in __import__("itertools").combinations(sorted(files), 2):
            joint[window][(a, b)] += 1
    for (a, b), count_recent in joint[True].items():
        count_old = joint[False].get((a, b), 0)
        if count_recent < 2 or count_old < 2:
            continue
        nr = _npmi(count_recent, marg[True][a], marg[True][b], pop[True])
        no = _npmi(count_old, marg[False][a], marg[False][b], pop[False])
        delta = (nr or 0) - (no or 0)
        trend = "emerging" if delta > 0.15 else "decaying" if delta < -0.15 else "stable"
        session.add(Drift(repo_id=repo_id, file_a_id=a, file_b_id=b, window_days=DRIFT_WINDOW_DAYS,
                           n_ab_recent=count_recent, n_ab_historic=count_old, npmi_recent=nr,
                           npmi_historic=no, delta=delta, trend=trend))


def _percentile(values: list[int], value: int) -> float:
    if len(values) <= 1:
        return 0.0
    return sum(x <= value for x in values) - 1


def _rebuild_risk(session: object, repo_id: int) -> None:
    File, Pair, AuthorFile, Risk = models().File, models().FilePair, models().AuthorFile, models().FileRisk
    session.query(Risk).filter_by(repo_id=repo_id).delete(synchronize_session=False)
    files = session.query(File).filter(File.repo_id == repo_id, File.change_count > 0).all()
    pairs = session.query(Pair).filter_by(repo_id=repo_id).all()
    partner_counts = defaultdict(int)
    for pair in pairs:
        partner_counts[pair.file_a_id] += 1
        partner_counts[pair.file_b_id] += 1
    links = session.query(AuthorFile).filter_by(repo_id=repo_id).all()
    authors = defaultdict(list)
    for link in links:
        authors[link.file_id].append(link.n_commits)
    changes = [f.change_count for f in files]
    partners = [partner_counts[f.id] for f in files]
    now = datetime.now(UTC)
    for file in files:
        counts = authors[file.id]
        total = sum(counts)
        hhi = sum((x / total) ** 2 for x in counts) if total else None
        churn_pct = _percentile(changes, file.change_count) / max(len(changes) - 1, 1)
        coupling_pct = _percentile(partners, partner_counts[file.id]) / max(len(partners) - 1, 1)
        evidence = file.change_count / (file.change_count + RISK_EVIDENCE_K)
        risk = churn_pct * coupling_pct * (1 + (hhi or 0)) * evidence
        days = int((now - file.last_change_at).total_seconds() // 86400) if file.last_change_at else None
        session.add(Risk(file_id=file.id, repo_id=repo_id, churn_pct=churn_pct, coupling_pct=coupling_pct,
                         ownership_hhi=hhi, effective_authors=(1 / hhi if hhi else None),
                         author_count=file.author_count, partner_count=partner_counts[file.id],
                         change_count=file.change_count, days_since_change=days, risk_score=risk))


def _refresh_mining_counts(session: object, stats: MiningStats) -> None:
    Drift, Risk = models().PairDrift, models().FileRisk
    drifts = session.query(Drift).all()
    stats.drift_rows = len(drifts)
    stats.emerging = sum(x.trend == "emerging" for x in drifts)
    stats.decaying = sum(x.trend == "decaying" for x in drifts)
    stats.risk_rows = session.query(Risk).count()


def cross_directory_modules(repo_id: int, limit: int = 20) -> list[dict]:
    Cluster, File = models().FileCluster, models().File
    with session_scope() as session:
        clusters = session.query(Cluster).filter(
            Cluster.repo_id == repo_id, Cluster.dirs_spanned > 1, Cluster.cluster_size >= 3
        ).all()
        grouped = defaultdict(list)
        for row in clusters:
            grouped[row.cluster_id].append(row)
        output = []
        for cluster_id, rows in grouped.items():
            files = session.query(File).filter(File.id.in_([x.file_id for x in rows])).all()
            output.append({"cluster_id": cluster_id, "cluster_size": rows[0].cluster_size,
                           "dirs_spanned": rows[0].dirs_spanned,
                           "avg_cohesion": round(sum(x.cohesion or 0 for x in rows) / len(rows), 3),
                           "directories": sorted({(f.dir_path or "").split("/")[0] for f in files}),
                           "sample_files": [f.path for f in sorted(files, key=lambda x: x.change_count, reverse=True)[:6]]})
        return sorted(output, key=lambda x: x["cluster_size"], reverse=True)[:limit]


def drifting_pairs(repo_id: int | None = None, trend: str = "emerging", limit: int = 25,
                   include_deleted: bool = False) -> list[dict]:
    Drift, File, Repo = models().PairDrift, models().File, models().Repo
    with session_scope() as session:
        query = session.query(Drift).filter_by(trend=trend)
        if repo_id is not None:
            query = query.filter_by(repo_id=repo_id)
        rows = query.all()
        files = {f.id: f for f in session.query(File).filter(File.id.in_([x for r in rows for x in (r.file_a_id, r.file_b_id)])).all()}
        repos = {r.id: r for r in session.query(Repo).filter(Repo.id.in_([r.repo_id for r in rows])).all()}
        output = []
        for row in rows:
            a, b = files.get(row.file_a_id), files.get(row.file_b_id)
            if not a or not b or (not include_deleted and (a.is_deleted or b.is_deleted)):
                continue
            value = _dict(row)
            value.update(path_a=a.path, path_b=b.path, repo=repos[row.repo_id].name)
            output.append(value)
        output.sort(key=lambda x: x["delta"], reverse=trend == "emerging")
        return output[:limit]


def risky_files(repo_id: int | None = None, limit: int = 25, include_deleted: bool = False) -> list[dict]:
    Risk, File, Repo, Link, Author = models().FileRisk, models().File, models().Repo, models().AuthorFile, models().Author
    with session_scope() as session:
        query = session.query(Risk).filter(Risk.change_count >= 5)
        if repo_id is not None:
            query = query.filter_by(repo_id=repo_id)
        rows = query.all()
        files = {f.id: f for f in session.query(File).filter(File.id.in_([x.file_id for x in rows])).all()}
        repos = {r.id: r for r in session.query(Repo).filter(Repo.id.in_([x.repo_id for x in rows])).all()}
        links = session.query(Link).filter(Link.file_id.in_([x.file_id for x in rows])).all()
        authors = {a.id: a for a in session.query(Author).filter(Author.id.in_([x.author_id for x in links])).all()}
        top = {}
        for link in links:
            if link.file_id not in top or link.n_commits > top[link.file_id].n_commits:
                top[link.file_id] = link
        output = []
        for row in rows:
            file = files.get(row.file_id)
            if not file or (file.is_deleted and not include_deleted):
                continue
            value = _dict(row)
            value.update(path=file.path, extension=file.extension,
                         repo=repos[row.repo_id].name, full_name=repos[row.repo_id].full_name,
                         top_author=authors[top[row.file_id].author_id].display_name if row.file_id in top else None)
            output.append(value)
        output.sort(key=lambda x: x.get("risk_score") or 0, reverse=True)
        return output[:limit]
