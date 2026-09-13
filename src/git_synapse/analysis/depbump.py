"""Dependency history extraction and declared dependency graph construction."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from git_synapse.analysis import manifests
from git_synapse.analysis.manifests import bounds, version_key
from git_synapse.db.orm import models, session_scope
from git_synapse.ingest.gitops import _base_env, mirror_path_for

log = logging.getLogger(__name__)
_MAJOR = re.compile(r"^v\d+$")
_REPO_PATH_ECOSYSTEMS = frozenset(("go", "actions", "docker", "bazel", "nix"))
_EXCLUDED_SEGMENTS = ("vendor/", "node_modules/", "testdata/", "third_party/", ".git/", "example/", "examples/", "docs/", "doc/", "website/")
MAX_MANIFESTS_PER_REPO = 200


def repo_ref(dep_name: str) -> tuple[str | None, str]:
    name = (dep_name or "").strip().strip("\"'").rstrip("/").lstrip("@")
    if "://" in name:
        name = name.split("://", 1)[1]
    name = name.split("#", 1)[0].removesuffix(".git")
    parts = [p for p in name.split("/") if p and not _MAJOR.match(p)]
    if not parts:
        return None, ""
    return (None, parts[0].lower()) if len(parts) == 1 else (parts[-2].lower(), parts[-1].lower())


def repo_key(dep_name: str) -> str:
    return repo_ref(dep_name)[1]


def manifest_paths(mirror: Path) -> list[tuple[str, str]]:
    proc = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=str(mirror), env=_base_env(),
                          capture_output=True, text=True, errors="replace", timeout=300)
    if proc.returncode != 0:
        return []
    excluded = {x.rstrip("/") for x in _EXCLUDED_SEGMENTS}
    found = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path or any(part in excluded for part in path.lower().split("/")[:-1]):
            continue
        ecosystem = manifests.ecosystem_for(path)
        if ecosystem is not None:
            found.append((path, ecosystem.name))
            if len(found) >= MAX_MANIFESTS_PER_REPO:
                break
    return found


def _blob_at(mirror: Path, sha: str, path: str) -> str:
    proc = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=str(mirror), env=_base_env(),
                          capture_output=True, text=True, errors="replace", timeout=120)
    return proc.stdout if proc.returncode == 0 else ""


def _snapshot(mirror: Path, sha: str, path: str) -> dict[str, manifests.Reference]:
    return {r.name: r for r in manifests.references(path, _blob_at(mirror, sha, path))}


def published_at_head(mirror: Path) -> set[tuple[str, str]]:
    found = set()
    for path, ecosystem in manifest_paths(mirror):
        for name in manifests.published_names(path, _blob_at(mirror, "HEAD", path)):
            found.add((ecosystem, name.lower()))
            tail = re.split(r"[:/]", name)[-1]
            if tail:
                found.add((ecosystem, tail.lower()))
    return found


def resolve_repo(dep_name: str, by_full_name: dict[tuple[str, str], int], by_name: dict[str, int],
                 by_package: dict[tuple[str, str], int] | None = None, ecosystem: str = "") -> int | None:
    if by_package and (hit := by_package.get((ecosystem, (dep_name or "").strip().lower()))):
        return hit
    if ecosystem and ecosystem not in _REPO_PATH_ECOSYSTEMS:
        return None
    owner, name = repo_ref(dep_name)
    if not name:
        return None
    return by_full_name.get((owner, name)) if owner is not None else by_name.get(name)


@dataclass
class BumpStats:
    repos_scanned: int = 0
    edges_found: int = 0
    edges_written: int = 0
    resolved_commits: int = 0
    duration_s: float = 0.0


@dataclass(slots=True)
class BumpEdge:
    consumer_sha: str
    dep_name: str
    dep_version: str
    dep_sha: str | None
    manifest: str
    ecosystem: str = ""


def extract_from_mirror(mirror: Path, repo_name: str, manifest: str = "go.mod", ecosystem: str = "go", max_commits: int = 400) -> list[BumpEdge]:
    proc = subprocess.run(["git", "log", "--all", "--no-merges", "--format=%H", "--", manifest], cwd=str(mirror), env=_base_env(),
                          capture_output=True, text=True, errors="replace", timeout=900)
    if proc.returncode != 0:
        return []
    revisions = list(reversed(proc.stdout.split()[:max_commits]))
    previous: dict[str, manifests.Reference] = {}
    edges = []
    for sha in revisions:
        current = _snapshot(mirror, sha, manifest)
        for name, ref in current.items():
            old = previous.get(name)
            if old is not None and old.raw == ref.raw:
                continue
            if repo_key(name) == repo_key(repo_name):
                continue
            edges.append(BumpEdge(sha, name, ref.raw, ref.sha, manifest, ecosystem))
        previous = current
    return edges


def declared_at_head(mirror: Path, repo_name: str, manifest: str = "go.mod", ecosystem: str = "go") -> list[tuple[str, str]]:
    return [(ref.name, ref.raw) for ref in _snapshot(mirror, "HEAD", manifest).values() if repo_key(ref.name) != repo_key(repo_name)]


def declared_modules_at_head(mirror: Path, repo_name: str, manifest: str) -> list[tuple[str, str, str]]:
    consumer = manifest.rsplit("/", 1)[0] if "/" in manifest else ""
    key = repo_key(repo_name)
    result = {}
    for ref in _snapshot(mirror, "HEAD", manifest).values():
        parts = ref.name.split("/")
        try:
            index = next(i for i, value in enumerate(parts) if value.lower() == key)
        except StopIteration:
            continue
        dep = "/".join(parts[index + 1:])
        if dep and dep != consumer:
            result.setdefault(dep, (consumer, dep, ref.raw))
    return list(result.values())


def _repo_lookups(session: object) -> tuple[dict[tuple[str, str], int], dict[str, int], dict[tuple[str, str], int]]:
    Repo, Package = models().Repo, models().RepoPackage
    # Ordered, because `by_full` below is a dict comprehension: when two
    # repositories share an owner and a name -- the same project mirrored on
    # two hosts, say -- the last row read wins, and an unordered query makes
    # that whichever one the database felt like returning last.
    rows = session.query(Repo.owner, Repo.name, Repo.id).order_by(Repo.id).all()
    by_full = {(str(owner).lower(), str(name).lower()): int(repo_id) for owner, name, repo_id in rows}
    grouped = defaultdict(set)
    for _, name, repo_id in rows:
        grouped[str(name).lower()].add(int(repo_id))
    by_name = {name: next(iter(ids)) for name, ids in grouped.items() if len(ids) == 1}
    claims = defaultdict(set)
    for ecosystem, name, repo_id in session.query(Package.ecosystem, Package.name, Package.repo_id).all():
        claims[(str(ecosystem), str(name).lower())].add(int(repo_id))
    by_package = {key: next(iter(ids)) for key, ids in claims.items() if len(ids) == 1}
    return by_full, by_name, by_package


def _record_packages(session: object, repo_id: int, claims: set[tuple[str, str]]) -> None:
    Package = models().RepoPackage
    session.query(Package).filter_by(repo_id=repo_id).delete(synchronize_session=False)
    session.add_all([Package(repo_id=repo_id, ecosystem=eco, name=name) for eco, name in sorted(claims)])


def _manifest_repos(session: object, force: bool, watermark: str) -> list[object]:
    Repo, File = models().Repo, models().File
    repos = session.query(Repo).filter(Repo.is_enabled.is_(True)).order_by(Repo.id).all()
    result = []
    for repo in repos:
        if not force and getattr(repo, watermark) == repo.head_sha and repo.head_sha is not None:
            continue
        if session.query(File.id).filter(File.repo_id == repo.id, File.basename.in_(list(manifests.MANIFEST_FILES))).first() is not None:
            result.append(repo)
    return result


def refresh_modules(conn: object | None = None) -> int:
    def run(session: object) -> int:
        Repo, Module = models().Repo, models().ModuleDependency
        repos = session.query(Repo).filter(Repo.is_enabled.is_(True)).order_by(Repo.id).all()
        payload, scanned = [], []
        for repo in repos:
            mirror = mirror_path_for(repo.full_name, host=repo.host)
            if not mirror.is_dir():
                continue
            scanned.append(repo.id)
            paths = [path for path, eco in manifest_paths(mirror) if eco == "go"]
            if len(paths) < 2:
                continue
            for path in paths:
                payload.extend((repo.id, consumer, dep, path, "go", version[:200])
                               for consumer, dep, version in declared_modules_at_head(mirror, repo.name, path))
        if scanned:
            session.query(Module).filter(Module.repo_id.in_(scanned)).delete(synchronize_session=False)
        seen = set()
        for row in payload:
            key = row[:4]
            if key not in seen:
                session.add(Module(repo_id=row[0], consumer_module=row[1], dep_module=row[2], manifest=row[3], ecosystem=row[4], dep_version=row[5]))
                seen.add(key)
        session.flush()
        return session.query(Module).count()
    if conn is not None:
        return run(conn)
    with session_scope() as session:
        return run(session)


def refresh_declared(conn: object | None = None, force: bool = False) -> int:
    def run(session: object) -> int:
        Repo, _File, Dependency = models().Repo, models().File, models().RepoDependency
        repos = _manifest_repos(session, force, "last_declared_sha")
        by_full, by_name, by_package = _repo_lookups(session)
        payload, scanned = [], []
        for repo in repos:
            mirror = mirror_path_for(repo.full_name, host=repo.host)
            if not mirror.is_dir():
                continue
            scanned.append(repo.id)
            for path, ecosystem in manifest_paths(mirror):
                for dep_name, version in declared_at_head(mirror, repo.name, path, ecosystem):
                    payload.append((repo.id, resolve_repo(dep_name, by_full, by_name, by_package, ecosystem), dep_name, version[:200], path, ecosystem))
        if scanned:
            session.query(Dependency).filter(Dependency.consumer_repo_id.in_(scanned)).delete(synchronize_session=False)
            seen = set()
            for row in payload:
                key = (row[0], row[2], row[4])
                if key in seen:
                    continue
                session.add(Dependency(consumer_repo_id=row[0], dep_repo_id=row[1], dep_name=row[2], dep_version=row[3], manifest=row[4], ecosystem=row[5]))
                seen.add(key)
            for repo in session.query(Repo).filter(Repo.id.in_(scanned)).all():
                repo.last_declared_sha = repo.head_sha
        # Resolve rows that were recorded before their publisher was indexed.
        for row in session.query(Dependency).filter(Dependency.dep_repo_id.is_(None)).all():
            candidate = resolve_repo(row.dep_name, by_full, by_name, by_package, row.ecosystem)
            if candidate is not None and candidate != row.consumer_repo_id:
                row.dep_repo_id = candidate
        session.flush()
        return session.query(Dependency).filter(Dependency.dep_repo_id.is_not(None)).count()
    if conn is not None:
        return run(conn)
    with session_scope() as session:
        return run(session)


def _repos_to_scan(conn: object, force: bool) -> list[object]:
    return _manifest_repos(conn, force, "last_depbump_sha")


def _ordinal(key: str | None) -> tuple[int, ...] | None:
    if not key or "-" in key:
        return None
    try:
        return tuple(int(part) for part in key.split("."))
    except ValueError:
        return None


def _link_repositories(session: object) -> int:
    by_full, by_name, by_package = _repo_lookups(session)
    linked = 0
    for bump in session.query(models().DepBump).filter(models().DepBump.dep_repo_id.is_(None)).all():
        repo_id = resolve_repo(bump.dep_name, by_full, by_name, by_package, bump.ecosystem)
        if repo_id is not None and repo_id != bump.consumer_repo_id:
            bump.dep_repo_id = repo_id
            linked += 1
    return linked


def _fill_version_keys(session: object) -> None:
    for bump in session.query(models().DepBump).filter(models().DepBump.version_key.is_(None)).all():
        floor, ceiling = bounds(bump.dep_version or "")
        if ceiling and not floor:
            continue
        bump.version_key = version_key(floor or bump.dep_version)


def _resolve_ceilings(session: object) -> int:
    Bump, Tag, Commit = models().DepBump, models().RefTag, models().Commit
    resolved = 0
    for bump in session.query(Bump).filter(Bump.dep_commit_id.is_(None), Bump.dep_repo_id.is_not(None)).all():
        floor, ceiling = bounds(bump.dep_version or "")
        limit = _ordinal(version_key(ceiling)) if ceiling and not floor else None
        if not limit:
            continue
        consumer = session.query(Commit).filter_by(repo_id=bump.consumer_repo_id, sha=bump.consumer_sha).first()
        if not consumer:
            continue
        choices = []
        for tag in session.query(Tag).filter(Tag.repo_id == bump.dep_repo_id, Tag.version_key.is_not(None)).all():
            commit_id = tag.commit_id or tag.main_commit_id
            if commit_id and (tag.tagged_at is None or tag.tagged_at <= consumer.committed_at):
                ordinal = _ordinal(tag.version_key)
                if ordinal and ordinal < limit:
                    choices.append((ordinal, commit_id))
        if choices:
            bump.dep_commit_id = max(choices)[1]
            bump.resolution = "ceiling"
            resolved += 1
    return resolved


def _reject_impossible(session: object) -> int:
    Bump, Commit = models().DepBump, models().Commit
    rejected = 0
    for bump in session.query(Bump).filter(Bump.dep_commit_id.is_not(None)).all():
        upstream = session.get(Commit, bump.dep_commit_id)
        consumer = session.query(Commit).filter_by(
            repo_id=bump.consumer_repo_id, sha=bump.consumer_sha
        ).first()
        if upstream and consumer and upstream.committed_at > consumer.committed_at:
            bump.dep_commit_id = None
            bump.resolution = None
            bump.adoption_seconds = None
            rejected += 1
    return rejected


def resolve_bumps(conn: object | None = None) -> int:
    def run(session: object) -> int:
        Bump, Tag, Commit = models().DepBump, models().RefTag, models().Commit
        # Callers commonly build or update bump/package rows in this same
        # transaction immediately before resolving them. Since the shared ORM
        # sessions use autoflush=False, make those writes visible to the lookup
        # queries explicitly.
        session.flush()
        _link_repositories(session)
        _fill_version_keys(session)
        for bump in session.query(Bump).filter(Bump.resolution.in_(["tag", "floor"])).all():
            bump.dep_commit_id = None
            bump.resolution = None
            bump.adoption_seconds = None
        # Queries below intentionally run with autoflush disabled. Persist the
        # invalidation before selecting pending rows, otherwise a previously
        # resolved bump is still filtered out by its old database values.
        session.flush()
        pending = session.query(Bump).filter(
            Bump.dep_commit_id.is_(None), Bump.dep_repo_id.is_not(None)
        ).all()
        consumer_keys = {(bump.consumer_repo_id, bump.consumer_sha) for bump in pending}
        consumers = {}
        for repo_id in {repo_id for repo_id, _ in consumer_keys}:
            shas = [sha for rid, sha in consumer_keys if rid == repo_id]
            consumers.update({(row.repo_id, row.sha): row for row in session.query(Commit).filter(
                Commit.repo_id == repo_id, Commit.sha.in_(shas)
            ).all()})
        for bump in pending:
            if bump.dep_sha:
                match = session.query(Commit).filter(
                    Commit.repo_id == bump.dep_repo_id,
                    Commit.sha.startswith(bump.dep_sha),
                ).first()
                if match:
                    bump.dep_commit_id, bump.resolution = match.id, "sha"
        tags = session.query(Tag).filter(Tag.version_key.is_not(None)).all()
        unique = defaultdict(set)
        for tag in tags:
            commit_id = tag.commit_id or tag.main_commit_id
            if commit_id:
                unique[(tag.repo_id, tag.version_key)].add(commit_id)
        for bump in session.query(Bump).filter(
            Bump.dep_commit_id.is_(None), Bump.dep_repo_id.is_not(None)
        ).all():
            ids = unique.get((bump.dep_repo_id, bump.version_key), set())
            if len(ids) == 1:
                bump.dep_commit_id = next(iter(ids))
                bump.resolution = "floor" if (bump.dep_version or "").startswith(("^", "~", ">", "<", "=")) else "tag"
        _resolve_ceilings(session)
        # `_reject_impossible` uses a query over resolved rows; flush the
        # in-memory matches first so future-dated candidates are actually
        # inspected when autoflush is disabled for this session.
        session.flush()
        _reject_impossible(session)
        session.flush()
        for bump in session.query(Bump).filter(Bump.dep_commit_id.is_not(None)).all():
            upstream = session.get(Commit, bump.dep_commit_id)
            downstream = consumers.get((bump.consumer_repo_id, bump.consumer_sha))
            if downstream is None:
                downstream = session.query(Commit).filter_by(
                    repo_id=bump.consumer_repo_id, sha=bump.consumer_sha
                ).first()
            if upstream and downstream:
                bump.adoption_seconds = int((downstream.committed_at - upstream.committed_at).total_seconds())
        return sum(x.dep_commit_id is not None for x in session.query(Bump).all())
    if conn is not None:
        return run(conn)
    with session_scope() as session:
        return run(session)


def rebuild(force: bool = False, conn: object | None = None) -> BumpStats:
    def run(session: object) -> BumpStats:
        started = time.monotonic()
        stats = BumpStats()
        _Repo, Bump, Commit = models().Repo, models().DepBump, models().Commit
        by_full, by_name, by_package = _repo_lookups(session)
        payload_repos = _repos_to_scan(session, force)
        for repo in payload_repos:
            mirror = mirror_path_for(repo.full_name, host=repo.host)
            if not mirror.is_dir():
                continue
            stats.repos_scanned += 1
            _record_packages(session, repo.id, published_at_head(mirror))
            by_full, by_name, by_package = _repo_lookups(session)
            edges = []
            for path, ecosystem in manifest_paths(mirror):
                edges.extend(extract_from_mirror(mirror, repo.name, path, ecosystem))
            stats.edges_found += len(edges)
            for edge in edges:
                dep_repo = resolve_repo(edge.dep_name, by_full, by_name, by_package, edge.ecosystem)
                exists = session.query(Bump).filter(
                    Bump.consumer_repo_id == repo.id, Bump.consumer_sha == edge.consumer_sha,
                    Bump.dep_name == edge.dep_name, Bump.dep_version == edge.dep_version,
                ).first()
                if exists:
                    continue
                consumer = session.query(Commit).filter_by(repo_id=repo.id, sha=edge.consumer_sha).first()
                dep_commit = next((c for c in session.query(Commit).filter_by(repo_id=dep_repo).all()
                                   if edge.dep_sha and c.sha.startswith(edge.dep_sha)), None) if dep_repo else None
                session.add(Bump(consumer_repo_id=repo.id, consumer_sha=edge.consumer_sha, dep_repo_id=dep_repo,
                                 dep_name=edge.dep_name, dep_version=edge.dep_version[:200], dep_sha=edge.dep_sha,
                                 dep_commit_id=dep_commit.id if dep_commit else None, manifest=edge.manifest,
                                 bumped_at=consumer.committed_at if consumer else None,
                                 resolution="sha" if dep_commit else None, ecosystem=edge.ecosystem))
                stats.edges_written += 1
            repo.last_depbump_at = datetime.now(UTC)
            repo.last_depbump_sha = repo.head_sha
        stats.resolved_commits = resolve_bumps(session)
        stats.duration_s = time.monotonic() - started
        return stats
    if conn is not None:
        return run(conn)
    with session_scope() as session:
        return run(session)


