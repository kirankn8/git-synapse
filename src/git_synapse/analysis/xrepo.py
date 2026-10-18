"""Cross-repository file coupling, built from the pins a repository declares.

Two repositories never share a commit, so within-repo coupling cannot reach across
them. A bump is the fact that can: the consumer commit names the upstream commit it
adopted, and the files on both sides of that link form the unit. Nothing here infers
a relationship from calendar time or a ticket convention.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass

from git_synapse.analysis.noise import is_noise
from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)

MAX_FILES_PER_SIDE = 200
#: A bump whose two sides would exceed this is a bulk change, not a relationship.
MAX_PAIRS_PER_BUMP = 5_000


@dataclass
class XrepoStats:
    consumers: int = 0
    bumps: int = 0
    pairs: int = 0
    skipped_bulk: int = 0
    duration_s: float = 0.0


def _interesting(path: str) -> bool:
    return not is_noise(path)


def _files(session: object, commit_id: int | None) -> list[tuple[int, str]]:
    if commit_id is None:
        return []
    CommitFile, File = models().CommitFile, models().File
    rows = session.query(File.id, File.path).join(
        CommitFile, CommitFile.file_id == File.id).filter(
        CommitFile.commit_id == commit_id).all()
    return [(file_id, path) for file_id, path in rows if _interesting(path)][:MAX_FILES_PER_SIDE]


def _consumer_commit_id(session: object, repo_id: int, sha: str) -> int | None:
    row = session.query(models().Commit.id).filter_by(repo_id=repo_id, sha=sha).first()
    return row[0] if row else None


def rebuild(consumer_ids: object | None = None, conn: object | None = None) -> XrepoStats:
    """Recompute the cross-repo file pairs for the given consumers, or for all of them."""
    def run(session: object) -> XrepoStats:
        started = time.monotonic()
        stats = XrepoStats()
        Bump, Pair = models().DepBump, models().XrepoFilePair
        scoped = {row[0] for row in session.query(Pair.consumer_repo_id).distinct().all()}
        if consumer_ids is not None:
            scoped |= set(consumer_ids)
        else:
            scoped |= {row[0] for row in session.query(Bump.consumer_repo_id).distinct().filter(
                Bump.dep_repo_id.is_not(None), Bump.dep_commit_id.is_not(None),
                Bump.tier != "transitive").all()}
        stats.consumers = len(scoped)

        # One consumer per transaction: its pairs appear while the rebuild
        # continues, and the pending rows never span the whole corpus.
        for consumer in sorted(scoped):
            bumps = session.query(Bump).filter(
                Bump.consumer_repo_id == consumer, Bump.dep_repo_id.is_not(None),
                Bump.dep_commit_id.is_not(None), Bump.tier != "transitive").all()
            session.query(Pair).filter(Pair.consumer_repo_id == consumer).delete(
                synchronize_session=False)
            counts: Counter = Counter()
            evidence: dict[tuple[int, int], dict] = {}
            for bump in bumps:
                consumer_files = _files(session, _consumer_commit_id(
                    session, bump.consumer_repo_id, bump.consumer_sha))
                upstream_files = _files(session, bump.dep_commit_id)
                if not consumer_files or not upstream_files:
                    continue
                if len(consumer_files) * len(upstream_files) > MAX_PAIRS_PER_BUMP:
                    stats.skipped_bulk += 1
                    continue
                stats.bumps += 1
                for upstream_id, _ in upstream_files:
                    for consumer_id, _ in consumer_files:
                        key = (upstream_id, consumer_id)
                        counts[key] += 1
                        seen = evidence.get(key)
                        if seen is None or (
                            bump.bumped_at is not None
                            and (seen["last_linked_at"] is None
                                 or bump.bumped_at > seen["last_linked_at"])
                        ):
                            evidence[key] = {
                                "upstream_repo_id": bump.dep_repo_id,
                                "last_version": bump.dep_version,
                                "last_linked_at": bump.bumped_at,
                            }
            rows = []
            for (upstream_id, consumer_id), n in counts.items():
                info = evidence[(upstream_id, consumer_id)]
                rows.append(Pair(upstream_file_id=upstream_id, consumer_file_id=consumer_id,
                                 upstream_repo_id=info["upstream_repo_id"],
                                 consumer_repo_id=consumer,
                                 n_bumps=n, first_linked_at=info["last_linked_at"],
                                 last_linked_at=info["last_linked_at"],
                                 last_version=info["last_version"]))
            session.add_all(rows)
            session.flush()
            session.commit()
            stats.pairs += len(rows)
        stats.duration_s = time.monotonic() - started
        return stats

    if conn is not None:
        return run(conn)
    with session_scope() as session:
        return run(session)


def coupled_across_repos(file_id: int, limit: int = 12, min_support: int = 2) -> list[dict]:
    """Files in other repositories linked to this one by a declared bump."""
    Pair, File, Repo = models().XrepoFilePair, models().File, models().Repo
    out: list[dict] = []
    with session_scope() as session:
        as_consumer = session.query(
            Repo.full_name, File.path, Pair.n_bumps, Pair.last_version, Pair.last_linked_at,
        ).join(File, File.id == Pair.upstream_file_id).join(
            Repo, Repo.id == Pair.upstream_repo_id).filter(
            Pair.consumer_file_id == file_id, Pair.n_bumps >= min_support,
        ).order_by(Pair.n_bumps.desc()).limit(limit).all()
        as_upstream = session.query(
            Repo.full_name, File.path, Pair.n_bumps, Pair.last_version, Pair.last_linked_at,
        ).join(File, File.id == Pair.consumer_file_id).join(
            Repo, Repo.id == Pair.consumer_repo_id).filter(
            Pair.upstream_file_id == file_id, Pair.n_bumps >= min_support,
        ).order_by(Pair.n_bumps.desc()).limit(limit).all()
    for direction, rows in (("upstream", as_consumer), ("downstream", as_upstream)):
        for repo, path, n, version, linked in rows:
            out.append({
                "direction": direction,
                "repo": repo,
                "path": path,
                "bumps": int(n),
                "version": version,
                "last_linked_at": linked.isoformat() if linked else None,
            })
    out.sort(key=lambda row: row["bumps"], reverse=True)
    return out[:limit]
