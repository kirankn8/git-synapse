"""Build aggregate fact tables from atomic commit/file ORM rows."""

from __future__ import annotations

import logging
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations

from git_synapse.config import get_config
from git_synapse.db.engine import connection
from git_synapse.db.orm import models

log = logging.getLogger(__name__)


@dataclass
class AggregateStats:
    repo_id: int
    files: int = 0
    directories: int = 0
    file_pairs: int = 0
    dir_pairs: int = 0
    author_files: int = 0
    pair_population: int = 0
    duration_s: float = 0.0


def _head_tree_paths(session: object, repo_id: int) -> set[str] | None:
    Repo = models().Repo
    repo = session.get(Repo, repo_id)
    if not repo:
        return None
    from git_synapse.ingest.gitops import mirror_path_for
    mirror = mirror_path_for(str(repo.full_name), host=str(repo.host))
    if not mirror.is_dir():
        return None
    try:
        result = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=str(mirror),
                                capture_output=True, text=True, errors="replace", timeout=300, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return {line for line in result.stdout.splitlines() if line} if result.returncode == 0 else None


def rebuild_repo(repo_id: int, conn: object | None = None) -> AggregateStats:
    def run(session: object) -> AggregateStats:
        started = time.monotonic()
        Commit, Change, File, Repo = models().Commit, models().CommitFile, models().File, models().Repo
        Directory, FD = models().Directory, models().FileDirectory
        FilePair, DirPair, AuthorFile = models().FilePair, models().DirPair, models().AuthorFile
        cfg = get_config()
        commits = session.query(Commit).filter_by(repo_id=repo_id).all()
        changes = session.query(Change).filter_by(repo_id=repo_id).all()
        files = session.query(File).filter_by(repo_id=repo_id).all()
        commit_by_id = {x.id: x for x in commits}
        by_commit: dict[int, set[int]] = defaultdict(set)
        changes_by_file: dict[int, list[object]] = defaultdict(list)
        for change in changes:
            by_commit[change.commit_id].add(change.file_id)
            changes_by_file[change.file_id].append(change)
        cap = cfg.ingest.max_files_per_commit
        eligible: dict[int, bool] = {}
        for commit in commits:
            commit.pair_eligible = eligible[commit.id] = (
                not commit.is_merge and not commit.is_replay and bool(by_commit[commit.id]) and
                (cap <= 0 or commit.n_files <= cap))
        population = sum(eligible.values())
        repo = session.get(Repo, repo_id)
        if repo:
            repo.pair_population = population

        # File marginals are deliberately calculated from the same commit set as pairs.
        for file in files:
            rows = changes_by_file.get(file.id, [])
            touched = [commit_by_id[x.commit_id] for x in rows]
            file.change_count = len(rows)
            file.pair_change_count = sum(eligible.get(x.id, False) for x in touched)
            file.insertions = sum(x.insertions or 0 for x in rows)
            file.deletions = sum(x.deletions or 0 for x in rows)
            authors = {x.author_id for x in touched if x.author_id is not None}
            file.author_count = len(authors)
            file.first_change_at = min((x.committed_at for x in touched), default=None)
            file.last_change_at = max((x.committed_at for x in touched), default=None)
        head_paths = _head_tree_paths(session, repo_id)
        for file in files:
            file.is_deleted = (file.path not in head_paths) if head_paths is not None else (
                bool(changes_by_file.get(file.id)) and changes_by_file[file.id][-1].change_type == "D")

        # Remove dependent materialisations before replacing their parent rows.
        for cls in (models().FilePairMetric, models().DirPairMetric, models().FilePair,
                    models().DirPair, models().AuthorFile):
            session.query(cls).filter_by(repo_id=repo_id).delete(synchronize_session=False)

        # Rebuild directory membership and remove directories that no longer own files.
        session.query(FD).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        old_dirs = session.query(Directory).filter_by(repo_id=repo_id).all()
        for directory in old_dirs:
            session.delete(directory)
        session.flush()
        dir_by_path: dict[str, object] = {}
        for file in files:
            parts = [p for p in (file.dir_path or "").split("/") if p]
            paths = [""] + ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
            for path in paths:
                directory = dir_by_path.get(path)
                if directory is None:
                    directory = Directory(repo_id=repo_id, path=path, depth=path.count("/") + (1 if path else 0))
                    session.add(directory)
                    session.flush()
                    dir_by_path[path] = directory
                session.add(FD(repo_id=repo_id, file_id=file.id, dir_id=directory.id))
        session.flush()
        dir_commits: dict[int, dict[int, list[object]]] = defaultdict(lambda: defaultdict(list))
        for change in changes:
            for directory in session.query(FD).filter_by(file_id=change.file_id).all():
                dir_commits[directory.dir_id][change.commit_id].append(change)
        for directory in dir_by_path.values():
            commit_rows = [commit_by_id[cid] for cid in dir_commits[directory.id]]
            directory.file_count = session.query(FD).filter_by(dir_id=directory.id).count()
            directory.change_count = len(commit_rows)
            directory.pair_change_count = sum(eligible.get(x.id, False) for x in commit_rows)
            directory.insertions = sum(sum(x.insertions or 0 for x in rows) for rows in dir_commits[directory.id].values())
            directory.deletions = sum(sum(x.deletions or 0 for x in rows) for rows in dir_commits[directory.id].values())
            directory.first_change_at = min((x.committed_at for x in commit_rows), default=None)
            directory.last_change_at = max((x.committed_at for x in commit_rows), default=None)

        # Materialised pair tables are replaced as one ORM transaction.
        now = datetime.now(UTC)
        half_life = max(cfg.analysis.recency_half_life_days, 1)
        min_support = max(cfg.ingest.min_pair_support, 1)
        file_pair_data: dict[tuple[int, int], list[object]] = defaultdict(list)
        for cid, ids in by_commit.items():
            if not eligible.get(cid):
                continue
            for a, b in combinations(sorted(ids), 2):
                file_pair_data[(a, b)].append(commit_by_id[cid])
        for (a, b), rows in file_pair_data.items():
            if len(rows) < min_support:
                continue
            weights = [2 ** (-max((now - row.committed_at).total_seconds(), 0) / 86400 / half_life) for row in rows]
            authors = {row.author_id for row in rows if row.author_id is not None}
            session.add(FilePair(repo_id=repo_id, file_a_id=a, file_b_id=b, n_ab=len(rows), w_ab=sum(weights),
                                 first_co_change=min(x.committed_at for x in rows), last_co_change=max(x.committed_at for x in rows),
                                 distinct_authors=len(authors)))

        dir_pair_data: dict[tuple[int, int], list[object]] = defaultdict(list)
        for cid, file_ids in by_commit.items():
            if not eligible.get(cid):
                continue
            dir_ids = {fd.dir_id for fid in file_ids for fd in session.query(FD).filter_by(file_id=fid).all()}
            for a, b in combinations(sorted(dir_ids), 2):
                dir_pair_data[(a, b)].append(commit_by_id[cid])
        for (a, b), rows in dir_pair_data.items():
            if len(rows) < min_support:
                continue
            weights = [2 ** (-max((now - row.committed_at).total_seconds(), 0) / 86400 / half_life) for row in rows]
            session.add(DirPair(repo_id=repo_id, dir_a_id=a, dir_b_id=b, n_ab=len(rows), w_ab=sum(weights),
                                first_co_change=min(x.committed_at for x in rows), last_co_change=max(x.committed_at for x in rows)))

        author_data: dict[tuple[int, int], list[object]] = defaultdict(list)
        for change in changes:
            commit = commit_by_id[change.commit_id]
            if commit.author_id is not None:
                author_data[(commit.author_id, change.file_id)].append((commit, change))
        for (author_id, file_id), rows in author_data.items():
            session.add(AuthorFile(repo_id=repo_id, author_id=author_id, file_id=file_id, n_commits=len(rows),
                                   insertions=sum(x.insertions or 0 for _, x in rows),
                                   deletions=sum(x.deletions or 0 for _, x in rows),
                                   first_at=min(x.committed_at for x, _ in rows), last_at=max(x.committed_at for x, _ in rows)))
        session.flush()
        if repo:
            repo.commit_count = len(commits)
            repo.file_count = len(files)
            repo.author_count = len({x.author_id for x in commits if x.author_id is not None})
            repo.pair_count = len(file_pair_data)
            repo.total_insertions = sum(x.insertions or 0 for x in commits)
            repo.total_deletions = sum(x.deletions or 0 for x in commits)
            repo.first_commit_at = min((x.committed_at for x in commits), default=None)
            repo.last_commit_at = max((x.committed_at for x in commits), default=None)
            repo.last_aggregate_at = now
        return AggregateStats(repo_id=repo_id, files=len(files), directories=len(dir_by_path),
                              file_pairs=sum(len(rows) >= min_support for rows in file_pair_data.values()),
                              dir_pairs=sum(len(rows) >= min_support for rows in dir_pair_data.values()),
                              author_files=len(author_data), pair_population=population,
                              duration_s=time.monotonic() - started)

    if conn is not None:
        return run(conn)
    with connection() as session:
        return run(session)


def repos_needing_aggregation(conn: object | None = None) -> list[int]:
    def run(session: object) -> list[int]:
        Repo, Pair, Metric = models().Repo, models().FilePair, models().FilePairMetric
        repos = session.query(Repo).filter(Repo.is_enabled.is_(True)).order_by(Repo.id).all()
        result = []
        for repo in repos:
            pair_count = session.query(Pair).filter_by(repo_id=repo.id).count()
            metric_count = session.query(Metric).filter_by(repo_id=repo.id).count()
            if repo.last_aggregate_at is None or repo.last_ingest_at is None or repo.last_aggregate_at < repo.last_ingest_at or pair_count != metric_count:
                result.append(repo.id)
        return result
    if conn is not None:
        return run(conn)
    with connection() as session:
        return run(session)
