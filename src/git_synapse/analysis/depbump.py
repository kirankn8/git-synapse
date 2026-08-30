"""Dependency-bump edges recovered from manifest history.

What this extracts
------------------
A Go pseudo-version embeds the upstream commit it was cut from::

    github.com/acme/signing/v3 v3.0.0-20260626221153-5fc63d6f3055
                                                       ^^^^^^^^^^^^

So a ``go.mod`` diff raising that module is a dated, **directional** statement:
"this consumer commit consumed that upstream commit". Unlike the co-occurrence
statistics elsewhere in this package, these rows are ground truth rather than
inference.

Why they exist here
-------------------
They *are* the cross-repository graph. That was not always so: they began as a
labelled set for measuring whether directed lagged statistics ranked real
propagation, and the answer was that they did not -- AUC 0.80 with 0.63 on
which way the arrow points, matched by a baseline that ignored coupling
altogether. The statistics went; the ground truth stayed and became the graph.

One number survives from that work and means something narrower than it used
to. The gap between the upstream commit and the bump that took it is a real
propagation delay, arithmetic on two known commit dates. It is not the earlier
sense of "lag" -- a time bin used to *infer* that two repositories were
related. Nothing infers a relationship from timing any more.

They are deliberately *not* used to replace the statistics: a manifest only
describes declared code dependencies, and misses the coupling that matters most
(Helm charts, docs, configs, tests). See the README for the measured comparison.

Incremental by repository
-------------------------
Scanning follows the same watermark pattern as aggregation: a repository is
rescanned only when ``last_ingest_at`` is newer than ``last_depbump_at``. Within
a repository the full ``go.mod`` history is re-walked, which is cheap, and the
insert is idempotent, so a rescan cannot duplicate rows.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import defaultdict
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import psycopg

from git_synapse.analysis import manifests
from git_synapse.analysis.manifests import bounds, version_key
from git_synapse.db.engine import connection, copy_rows
from git_synapse.ingest.gitops import _base_env, mirror_path_for

log = logging.getLogger(__name__)

#: Go appends a major-version segment to a module path; it is not a repository.
_MAJOR = re.compile(r"^v\d+$")


def repo_ref(dep_name: str) -> tuple[str | None, str]:
    """Split a dependency reference into ``(owner, name)``.

    Every ecosystem spells the same repository differently --
    ``github.com/acme/signer``, ``@acme/signer``, ``acme/signer``, ``signer`` --
    but the last two path segments are the owner and the repository in all of
    them. The owner is None when the reference carries none, as with a bare
    Cargo crate or an unscoped npm package.
    """
    name = (dep_name or "").strip().strip("\"'").rstrip("/").lstrip("@")
    if "://" in name:
        name = name.split("://", 1)[1]
    name = name.split("#", 1)[0].removesuffix(".git")
    parts = [p for p in name.split("/") if p and not _MAJOR.match(p)]
    if not parts:
        return (None, "")
    if len(parts) == 1:
        return (None, parts[0].lower())
    return (parts[-2].lower(), parts[-1].lower())


def repo_key(dep_name: str) -> str:
    """Just the repository name, for callers that cannot know the owner."""
    return repo_ref(dep_name)[1]


def published_at_head(mirror: Path) -> set[tuple[str, str]]:
    """Every `(ecosystem, coordinate)` this repository publishes.

    A monorepo publishes many, so this is a set. Both the full coordinate and
    its last segment are recorded, because a consumer may write either: Maven's
    pom names `com.google.guava:guava` while the dependency block consuming it
    writes `guava`.

    The ecosystem travels with the name and is not decoration. `illuminate/events`
    is a PHP package published by laravel/framework and `events` is an unrelated
    npm one; without the scope, every npm dependency on `events` became an edge
    into a PHP repository.
    """
    found: set[tuple[str, str]] = set()
    for path, ecosystem in manifest_paths(mirror):
        text = _blob_at(mirror, "HEAD", path)
        for full in manifests.published_names(path, text):
            found.add((ecosystem, full.lower()))
            if tail := re.split(r"[:/]", full)[-1]:
                found.add((ecosystem, tail.lower()))
    return found


#: Ecosystems whose coordinate *is* a repository reference. A Go module path is
#: host/owner/repo, a GitHub Action is owner/repo, a submodule is a URL -- so
#: reading a repository out of the name is reading what it says.
#:
#: Everywhere else the coordinate names a registry artifact, and matching it
#: against repository names invents edges: npm's `uuid` became google/uuid, a Go
#: library, and npm's `bytes` became tokio-rs/bytes, a Rust crate. Those
#: resolved to no commit only because the versions could never match, which is
#: luck rather than a guard.
_REPO_PATH_ECOSYSTEMS = frozenset(("go", "actions", "docker", "bazel", "nix"))


def resolve_repo(dep_name: str, by_full_name: dict[tuple[str, str], int],
                 by_name: dict[str, int],
                 by_package: dict[tuple[str, str], int] | None = None,
                 ecosystem: str = "") -> int | None:
    """The indexed repository a reference names, or None.

    What a repository publishes is checked first, because that is a fact it
    declared about itself rather than an inference from the strings agreeing.

    The fallback is owner-aware on purpose. Matching on the repository name
    alone would make ``gitlab.com/otherco/utils`` resolve to an indexed
    ``acme/utils`` -- an unrelated company's library becoming an edge into this
    codebase. The name alone is only trusted when the reference genuinely
    carries no owner.
    """
    if by_package and (hit := by_package.get((ecosystem, (dep_name or "").strip().lower()))):
        return hit
    if ecosystem and ecosystem not in _REPO_PATH_ECOSYSTEMS:
        # A registry coordinate that nothing published says it owns. Guessing
        # from the name is how an npm package becomes a Go repository.
        return None
    owner, name = repo_ref(dep_name)
    if not name:
        return None
    if owner is not None:
        return by_full_name.get((owner, name))
    return by_name.get(name)


#: Path prefixes and segments whose manifests describe third-party or fixture
#: code rather than this repository's own dependencies.
#: `docs/` earns its place here: django ships `docs/ref/models/constraints.txt`,
#: which is prose about database constraints, and reading it as a pip
#: constraints file invents dependencies out of English.
_EXCLUDED_SEGMENTS = ("vendor/", "node_modules/", "testdata/", "third_party/",
                      ".git/", "example/", "examples/", "docs/", "doc/", "website/")

#: Ceiling on manifests read per repository. A monorepo legitimately has dozens;
#: anything past this is a vendored tree that slipped the filter.
MAX_MANIFESTS_PER_REPO = 200


def _record_packages(conn: psycopg.Connection, repo_id: int, claims: set[tuple[str, str]]) -> None:
    """Replace what this repository is known to publish."""
    conn.execute("DELETE FROM repo_package WHERE repo_id = %s", (repo_id,))
    if claims:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO repo_package (repo_id, ecosystem, name) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                [(repo_id, eco, name) for eco, name in sorted(claims)])


def _repo_lookups(conn: psycopg.Connection) -> tuple[dict[tuple[str, str], int], dict[str, int], dict[str, int]]:
    """Repositories by (owner, name), by name, and by what they publish.

    A coordinate two repositories both claim is dropped rather than resolved to
    whichever came first: two projects publishing an artifact called `core` is
    ordinary, and picking one would invent an edge.
    """
    rows = conn.execute("SELECT owner, name, id FROM repo").fetchall()
    by_full = {(str(o).lower(), str(n).lower()): int(i) for o, n, i in rows}
    by_name = {str(n).lower(): int(i) for _, n, i in rows}

    claims: dict[tuple[str, str], set[int]] = defaultdict(set)
    for eco, name, repo_id in conn.execute(
            "SELECT ecosystem, name, repo_id FROM repo_package").fetchall():
        claims[(str(eco), str(name).lower())].add(int(repo_id))
    by_package = {k: next(iter(v)) for k, v in claims.items() if len(v) == 1}
    return by_full, by_name, by_package


def manifest_paths(mirror: Path) -> list[tuple[str, str]]:
    """Every manifest at HEAD, anywhere in the tree, as ``(path, ecosystem)``.

    Reading only the repository root was a real coverage gap, not a simplifying
    assumption: monorepos keep their real dependencies in per-module manifests,
    and a repository with fourteen go.mod files below the root looked like one
    with no internal dependencies at all.
    """
    proc = subprocess.run(  # noqa: S603 - fixed executable
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=str(mirror), env=_base_env(), capture_output=True,
        text=True, errors="replace", timeout=300,
    )
    if proc.returncode != 0:
        return []

    found: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        lowered = path.lower()
        if any(seg in lowered for seg in _EXCLUDED_SEGMENTS):
            continue
        eco = manifests.ecosystem_for(path)
        if eco is None:
            continue
        found.append((path, eco.name))
        if len(found) >= MAX_MANIFESTS_PER_REPO:
            log.warning("manifest cap reached in %s; ignoring the rest", mirror.name)
            break
    return found


def _blob_at(mirror: Path, sha: str, path: str) -> str:
    """One file as it stood at one commit, or "" if it was not there."""
    proc = subprocess.run(  # noqa: S603 - fixed executable
        ["git", "show", f"{sha}:{path}"],
        cwd=str(mirror), env=_base_env(), capture_output=True,
        text=True, errors="replace", timeout=120,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _snapshot(mirror: Path, sha: str, path: str) -> dict[str, manifests.Reference]:
    """Every reference a manifest declared at one commit, keyed by name."""
    return {r.name: r for r in manifests.references(path, _blob_at(mirror, sha, path))}


@dataclass
class BumpStats:
    """Outcome of one bump-extraction pass."""

    repos_scanned: int = 0
    edges_found: int = 0
    edges_written: int = 0
    resolved_commits: int = 0
    duration_s: float = 0.0


@dataclass(slots=True)
class BumpEdge:
    """One manifest line raising an internal dependency."""

    consumer_sha: str
    dep_name: str
    dep_version: str
    dep_sha: str | None
    manifest: str
    ecosystem: str = ""


def extract_from_mirror(mirror: Path, repo_name: str, manifest: str = "go.mod", ecosystem: str = "go", max_commits: int = 400) -> list[BumpEdge]:
    """Walk a manifest's history and return every version change it records.

    Compares whole-file snapshots at consecutive commits rather than reading
    added diff lines. A diff line carries no context -- ``version = "1.2.3"``
    says nothing about which package it belongs to -- which is why the line
    reading only ever worked for go.mod and package.json. Parsing the file at
    each commit that touched it costs one `git show` per revision and works for
    every format, structured ones included.
    """
    proc = subprocess.run(  # noqa: S603 - fixed executable
        ["git", "log", "--all", "--no-merges", "--format=%H", "--", manifest],
        cwd=str(mirror), env=_base_env(), capture_output=True,
        text=True, errors="replace", timeout=900,
    )
    if proc.returncode != 0:
        log.warning("manifest scan failed for %s: %s", repo_name, proc.stderr[:200])
        return []

    # Oldest first, so each snapshot is compared against what preceded it.
    revisions = list(reversed(proc.stdout.split()))[:max_commits]
    edges: list[BumpEdge] = []
    previous: dict[str, manifests.Reference] = {}
    for sha in revisions:
        current = _snapshot(mirror, sha, manifest)
        for name, ref in current.items():
            was = previous.get(name)
            if was is not None and was.raw == ref.raw:
                continue                      # unchanged at this commit
            if repo_key(name) == repo_key(repo_name):
                continue                      # the module naming itself
            edges.append(BumpEdge(
                consumer_sha=sha, dep_name=name, dep_version=ref.raw,
                dep_sha=ref.sha, manifest=manifest, ecosystem=ecosystem))
        previous = current
    return edges


def declared_at_head(mirror: Path, repo_name: str, manifest: str = "go.mod", ecosystem: str = "go") -> list[tuple[str, str]]:
    """Dependencies declared in ``manifest`` at HEAD.

    Present-tense and structural, in contrast to :func:`extract_from_mirror`
    which reads history. This is the candidate set for impact prediction.
    """
    refs = _snapshot(mirror, "HEAD", manifest)
    return [(r.name, r.raw) for r in refs.values() if repo_key(r.name) != repo_key(repo_name)]


def declared_modules_at_head(mirror: Path, repo_name: str, manifest: str) -> list[tuple[str, str, str]]:
    """Intra-repository module dependencies declared in one manifest.

    Returns ``(consumer_module, dep_module, version)`` for every reference the
    manifest makes to another module of the *same* repository. These are the
    references :func:`declared_at_head` skips as self-references, and in a
    monorepo they are the whole structural graph.

    ``consumer_module`` is the manifest's own directory, so ``gateway/go.mod``
    yields ``gateway``. A module declaring itself is dropped.
    """
    consumer = manifest.rsplit("/", 1)[0] if "/" in manifest else ""
    key = repo_key(repo_name)
    out: dict[str, tuple[str, str, str]] = {}
    for ref in _snapshot(mirror, "HEAD", manifest).values():
        segments = ref.name.split("/")
        # Find where this repository's own name appears; what follows is the
        # module path inside it. Anything else is a cross-repo dependency.
        try:
            at = next(i for i, seg in enumerate(segments) if seg.lower() == key)
        except StopIteration:
            continue
        dep_module = "/".join(segments[at + 1:])
        if not dep_module or dep_module == consumer:
            continue
        out.setdefault(dep_module, (consumer, dep_module, ref.raw))
    return list(out.values())


def refresh_modules(conn: psycopg.Connection | None = None) -> int:
    """Rebuild ``module_dependency`` from every repository's manifests at HEAD.

    Full rebuild for the same reason as :func:`refresh_declared`: a dependency
    removed from a manifest has to disappear, which an upsert would never do.
    """

    def _run(c: psycopg.Connection) -> int:
        rows = c.execute(
            "SELECT id, full_name, name FROM repo WHERE is_enabled ORDER BY id"
        ).fetchall()

        payload = []
        for repo_id, full_name, name in rows:
            mirror = mirror_path_for(full_name)
            if not mirror.is_dir():
                continue
            manifests = [m for m, eco in manifest_paths(mirror) if eco == "go"]
            if len(manifests) < 2:
                continue  # a single-module repo has no internal graph
            for manifest in manifests:
                for consumer, dep, version in declared_modules_at_head(mirror, name, manifest):
                    payload.append(
                        (repo_id, consumer, dep, manifest, "go", version[:200])
                    )

        c.execute("TRUNCATE module_dependency")
        if payload:
            c.execute(
                """
                CREATE TEMP TABLE tmp_mod (
                    repo_id BIGINT, consumer_module TEXT, dep_module TEXT,
                    manifest TEXT, ecosystem TEXT, dep_version TEXT
                ) ON COMMIT DROP
                """
            )
            copy_rows(
                "tmp_mod",
                ["repo_id", "consumer_module", "dep_module", "manifest",
                 "ecosystem", "dep_version"],
                payload,
                conn=c,
            )
            c.execute(
                """
                INSERT INTO module_dependency (repo_id, consumer_module, dep_module,
                                               manifest, ecosystem, dep_version)
                SELECT DISTINCT ON (repo_id, consumer_module, dep_module, manifest)
                       repo_id, consumer_module, dep_module, manifest,
                       ecosystem, dep_version
                FROM tmp_mod
                ON CONFLICT DO NOTHING
                """
            )
            c.execute("DROP TABLE IF EXISTS tmp_mod")

        total = int(c.execute("SELECT count(*) FROM module_dependency").fetchone()[0])
        repos = int(
            c.execute(
                "SELECT count(DISTINCT repo_id) FROM module_dependency"
            ).fetchone()[0]
        )
        log.info("module graph: %d edges across %d multi-module repos", total, repos)
        return total

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def refresh_declared(
    conn: psycopg.Connection | None = None, force: bool = False
) -> int:
    """Rebuild ``repo_dependency`` from each repo's HEAD manifest.

    Per repository this is a full re-read rather than a delta, and deliberately
    so: a dependency *removed* from a manifest has to disappear, which an
    incremental upsert would never achieve. But a repository whose HEAD has not
    moved cannot have changed its manifest, so only repositories ingested since
    their last scan are re-read.
    """

    def _run(c: psycopg.Connection) -> int:
        stale = "" if force else (
            " AND (r.last_declared_sha IS NULL"
            " OR r.head_sha IS NULL"
            " OR r.last_declared_sha <> r.head_sha)"
        )
        rows = c.execute(
            f"""
            SELECT r.id, r.full_name, r.name FROM repo r
            WHERE r.is_enabled
              AND EXISTS (SELECT 1 FROM file f
                           WHERE f.repo_id = r.id AND f.basename = ANY(%(manifests)s))
              {stale}
            ORDER BY r.id
            """,
            {"manifests": list(manifests.MANIFEST_FILES)},
        ).fetchall()
        if not rows:
            total = int(c.execute("SELECT count(*) FROM repo_dependency"
                                  " WHERE dep_repo_id IS NOT NULL").fetchone()[0])
            log.info("declared dependencies: no repository manifests changed")
            return total
        by_full, by_name, by_pkg = _repo_lookups(c)

        payload = []
        for repo_id, full_name, name in rows:
            mirror = mirror_path_for(full_name)
            if not mirror.is_dir():
                continue
            for manifest, ecosystem in manifest_paths(mirror):
                for dep_name, version in declared_at_head(mirror, name, manifest, ecosystem):
                    payload.append(
                        (repo_id,
                         resolve_repo(dep_name, by_full, by_name, by_pkg, ecosystem),
                         dep_name, version[:200], manifest, ecosystem)
                    )

        # Delete only the scanned repositories' rows, so an incremental pass
        # leaves every other repository's declared set intact.
        c.execute(
            "DELETE FROM repo_dependency WHERE consumer_repo_id = ANY(%s)",
            ([r[0] for r in rows],),
        )
        if payload:
            c.execute(
                """
                CREATE TEMP TABLE tmp_dep (
                    consumer_repo_id BIGINT, dep_repo_id BIGINT, dep_name TEXT,
                    dep_version TEXT, manifest TEXT, ecosystem TEXT
                ) ON COMMIT DROP
                """
            )
            copy_rows(
                "tmp_dep",
                ["consumer_repo_id", "dep_repo_id", "dep_name", "dep_version",
                 "manifest", "ecosystem"],
                payload,
                conn=c,
            )
            c.execute(
                """
                INSERT INTO repo_dependency (consumer_repo_id, dep_repo_id, dep_name,
                                             dep_version, manifest, ecosystem)
                SELECT DISTINCT ON (consumer_repo_id, dep_name, manifest)
                       consumer_repo_id, dep_repo_id, dep_name, dep_version,
                       manifest, ecosystem
                FROM tmp_dep
                ON CONFLICT DO NOTHING
                """
            )
            c.execute("DROP TABLE IF EXISTS tmp_dep")

        # Record the watermark this pass consumed, so the next one can skip
        # repositories whose HEAD has not moved.
        c.execute(
            "UPDATE repo SET last_declared_sha = head_sha WHERE id = ANY(%s)",
            ([r[0] for r in rows],),
        )

        total = int(c.execute("SELECT count(*) FROM repo_dependency").fetchone()[0])
        internal = int(
            c.execute(
                "SELECT count(*) FROM repo_dependency WHERE dep_repo_id IS NOT NULL"
            ).fetchone()[0]
        )
        log.info(
            "declared dependencies: %d rows (%d resolve to a tracked repository)",
            total, internal,
        )
        return internal

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def _repos_to_scan(conn: psycopg.Connection, force: bool) -> list[tuple[int, str, str]]:
    """Repositories with a manifest whose bump scan is stale.

    Staleness is judged on the HEAD sha, not a timestamp. ``last_ingest_at``
    advances on every run whether or not commits landed, so a timestamp
    comparison was always true and every one of the 187 manifest-bearing
    repositories was re-scanned nightly for no reason.
    """
    clause = "" if force else (
        " AND (r.last_depbump_sha IS NULL"
        " OR r.head_sha IS NULL"
        " OR r.last_depbump_sha <> r.head_sha)"
    )
    rows = conn.execute(
        f"""
        SELECT r.id, r.full_name, r.name
        FROM repo r
        WHERE r.is_enabled
          AND EXISTS (SELECT 1 FROM file f
                       WHERE f.repo_id = r.id AND f.basename = ANY(%(manifests)s))
          {clause}
        ORDER BY r.id
        """,
        {"manifests": list(manifests.MANIFEST_FILES)},
    ).fetchall()
    return [(int(r[0]), r[1], r[2]) for r in rows]


def resolve_bumps(conn: psycopg.Connection | None = None) -> int:
    """Fill in the upstream commit for bumps that do not yet have one.

    Separate from extraction, and re-runnable, because the inputs arrive at
    different times: a bump is recorded the moment a manifest changes, but the
    tag that dates it may only be mirrored later, and the upstream commit may
    only be ingested later still.

    Four tiers, strongest first, each recorded in ``resolution`` so a weaker one
    is never read as an exact answer:

    ``sha``      the manifest named the commit outright.
    ``tag``      an exact version matched a tag.
    ``floor``    a range's declared lower bound matched a tag. Says only "at
                 least these commits arrived": what was installed may have
                 drifted higher, but a manifest nobody edited is one where
                 nothing had to adapt.
    ``ceiling``  an upper bound with no floor, resolved to the newest release
                 below it that existed when the bump was made.

    Returns:
        How many rows gained a commit.
    """

    def _run(c: psycopg.Connection) -> int:
        linked = _link_repositories(c)
        _fill_version_keys(c)

        # Pins first: a reference naming a commit needs no interpretation.
        by_sha = c.execute(
            """
            UPDATE dep_bump b
               SET dep_commit_id = dc.id, resolution = 'sha'
              FROM commit dc
             WHERE b.dep_commit_id IS NULL
               AND b.dep_sha IS NOT NULL
               AND dc.repo_id = b.dep_repo_id
               AND dc.sha LIKE b.dep_sha || '%'
            """
        ).rowcount or 0

        # Then releases, by canonical key. `commit_id` is the tagged commit
        # when the walk read it; `main_commit_id` is the shipping-branch commit
        # the release was cut from, which is the only one that exists when a
        # project tags on a release branch.
        by_tag = c.execute(
            r"""
            UPDATE dep_bump b
               SET dep_commit_id = COALESCE(rt.commit_id, rt.main_commit_id),
                   resolution = CASE WHEN b.dep_version ~ '^[\^~><=]'
                                     THEN 'floor' ELSE 'tag' END
              FROM ref_tag rt
             WHERE b.dep_commit_id IS NULL
               AND b.version_key IS NOT NULL
               AND rt.repo_id = b.dep_repo_id
               AND rt.version_key = b.version_key
               AND COALESCE(rt.commit_id, rt.main_commit_id) IS NOT NULL
            """
        ).rowcount or 0

        by_ceiling = _resolve_ceilings(c)
        rejected = _reject_impossible(c)

        c.execute(
            """
            UPDATE dep_bump b
               SET adoption_seconds = EXTRACT(EPOCH FROM (cc.committed_at - dc.committed_at))::bigint
              FROM commit dc, commit cc
             WHERE b.adoption_seconds IS NULL
               AND b.dep_commit_id = dc.id
               AND cc.repo_id = b.consumer_repo_id
               AND cc.sha = b.consumer_sha
            """
        )
        log.info("bump resolution: %d newly linked to a repository, %d by pinned "
                 "sha, %d by tag or floor, %d by ceiling; %d rejected as impossible",
                 linked, by_sha, by_tag, by_ceiling, rejected)
        return by_sha + by_tag + by_ceiling

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def _link_repositories(c: psycopg.Connection) -> int:
    """Attach bumps to the repository that publishes what they name.

    Re-runnable like the rest of this pass, and for the same reason: a bump is
    recorded before the repository publishing it is necessarily indexed, and a
    coordinate only becomes attributable once that repository's own manifests
    have been read.

    A coordinate two repositories both claim is left alone, and so is one
    naming the consumer itself -- an intra-repository reference is a module
    edge, not a dependency between repositories.
    """
    return c.execute(
        """
        UPDATE dep_bump b
           SET dep_repo_id = p.repo_id
          FROM (SELECT ecosystem, name, min(repo_id) AS repo_id
                  FROM repo_package GROUP BY ecosystem, name
                HAVING count(DISTINCT repo_id) = 1) p
         WHERE b.dep_repo_id IS NULL
           AND lower(b.dep_name) = p.name
           AND b.ecosystem = p.ecosystem
           AND p.repo_id <> b.consumer_repo_id
        """
    ).rowcount or 0


def _fill_version_keys(c: psycopg.Connection) -> None:
    """Give every bump the canonical key it should be matched on.

    For an exact version that is the version itself; for a range it is the
    declared floor. Computed here rather than in SQL because one parser has to
    serve both sides of the match, and it already exists.
    """
    rows = c.execute(
        "SELECT DISTINCT dep_version FROM dep_bump "
        " WHERE version_key IS NULL AND dep_version <> ''").fetchall()
    keyed = []
    for (raw,) in rows:
        floor, ceiling = bounds(raw)
        # An upper bound names a version that was explicitly *excluded*. Keying
        # on it would match the one release we know was never taken.
        if ceiling and not floor:
            continue
        if key := version_key(floor or raw):
            keyed.append((key, raw))
    if keyed:
        with c.cursor() as cur:
            cur.executemany(
                "UPDATE dep_bump SET version_key = %s "
                " WHERE version_key IS NULL AND dep_version = %s", keyed)


def _resolve_ceilings(c: psycopg.Connection) -> int:
    """Resolve `<3.0` to the newest release below it that already existed.

    Ordering versions is the reason this is not SQL: `1.10` sorts below `1.9`
    as text and above it as a version. Bounded by the bump's own date so the
    answer cannot drift as later tags arrive, and so it can never name a
    release published after the commit that consumed it.
    """
    rows = c.execute(
        """
        SELECT b.ctid, b.dep_version, b.dep_repo_id, cc.committed_at
          FROM dep_bump b
          JOIN commit cc ON cc.repo_id = b.consumer_repo_id AND cc.sha = b.consumer_sha
         WHERE b.dep_commit_id IS NULL AND b.dep_repo_id IS NOT NULL
        """
    ).fetchall()

    resolved = []
    for ctid, raw, dep_repo_id, bumped_at in rows:
        floor, ceiling = bounds(raw)
        if floor or not ceiling or not (limit := _ordinal(version_key(ceiling))):
            continue
        candidates = c.execute(
            """
            SELECT version_key, COALESCE(commit_id, main_commit_id) AS cid
              FROM ref_tag
             WHERE repo_id = %s AND version_key IS NOT NULL
               AND COALESCE(commit_id, main_commit_id) IS NOT NULL
               AND (tagged_at IS NULL OR tagged_at <= %s)
            """, (dep_repo_id, bumped_at)).fetchall()
        below = [(o, cid) for key, cid in candidates
                 if (o := _ordinal(key)) and o < limit]
        if below:
            resolved.append((max(below)[1], ctid))
    if resolved:
        with c.cursor() as cur:
            cur.executemany("UPDATE dep_bump SET dep_commit_id = %s, "
                            "resolution = 'ceiling' WHERE ctid = %s", resolved)
    return len(resolved)


def _ordinal(key: str | None) -> tuple[int, ...] | None:
    """A version key as comparable numbers, or None when it is a prerelease.

    A prerelease sorts below its own release and has no total order against
    other prereleases worth relying on, so it is simply not a candidate for
    "the newest release below this bound".
    """
    if not key or "-" in key:
        return None
    try:
        return tuple(int(part) for part in key.split("."))
    except ValueError:
        return None


def _reject_impossible(c: psycopg.Connection) -> int:
    """Undo any resolution naming a commit written after the bump consumed it.

    Nothing can depend on a commit that does not exist yet, so a violation is
    proof the match is wrong -- a bad key, a bad repo mapping, or a tag that
    moved. Cheaper and more general than trying to enumerate the ways each
    could happen.
    """
    return c.execute(
        """
        UPDATE dep_bump b
           SET dep_commit_id = NULL, resolution = NULL, adoption_seconds = NULL
          FROM commit dc, commit cc
         WHERE b.dep_commit_id = dc.id
           AND cc.repo_id = b.consumer_repo_id
           AND cc.sha = b.consumer_sha
           AND dc.committed_at > cc.committed_at
        """
    ).rowcount or 0


def rebuild(force: bool = False, conn: psycopg.Connection | None = None) -> BumpStats:
    """Extract manifest-bump edges for every stale repository.

    Args:
        force: rescan every repository with a manifest, ignoring watermarks.
        conn: reuse an open connection.
    """

    def _run(c: psycopg.Connection) -> BumpStats:
        started = time.monotonic()
        stats = BumpStats()

        targets = _repos_to_scan(c, force)
        if not targets:
            log.debug("no repositories need a manifest scan")
            return stats

        # Built once, so a dependency resolves without a query per edge.
        by_full, by_name, by_pkg = _repo_lookups(c)

        payload: list[tuple] = []
        for repo_id, full_name, name in targets:
            mirror = mirror_path_for(full_name)
            if not mirror.is_dir():
                continue
            stats.repos_scanned += 1
            edges: list[BumpEdge] = []
            _record_packages(c, repo_id, published_at_head(mirror))
            for manifest, ecosystem in manifest_paths(mirror):
                edges.extend(extract_from_mirror(mirror, name, manifest, ecosystem))
            stats.edges_found += len(edges)
            for edge in edges:
                payload.append(
                    (
                        repo_id,
                        edge.consumer_sha,
                        resolve_repo(edge.dep_name, by_full, by_name, by_pkg, edge.ecosystem),
                        edge.dep_name,
                        edge.dep_version[:200],
                        edge.dep_sha,
                        edge.manifest,
                        edge.ecosystem,
                    )
                )

        if payload:
            c.execute(
                """
                CREATE TEMP TABLE tmp_bump (
                    consumer_repo_id BIGINT, consumer_sha TEXT, dep_repo_id BIGINT,
                    dep_name TEXT, dep_version TEXT, dep_sha TEXT, manifest TEXT,
                    ecosystem TEXT
                ) ON COMMIT DROP
                """
            )
            copy_rows(
                "tmp_bump",
                ["consumer_repo_id", "consumer_sha", "dep_repo_id", "dep_name",
                 "dep_version", "dep_sha", "manifest", "ecosystem"],
                payload,
                conn=c,
            )
            # Only what is free at insert time: the consumer's timestamp, and
            # the dependency commit when the manifest named it outright. A
            # pseudo-version sha is a 12-char prefix, so that join is a prefix
            # match.
            #
            # Version-to-tag matching deliberately does *not* happen here. It
            # lived in this statement as a list of spellings to try, which
            # duplicated the resolution pass, could not see a release tagged off
            # the shipping branch, knew nothing of ranges, and recorded no tier --
            # so a row resolved here was indistinguishable from one resolved
            # exactly. `resolve_bumps` owns it, and is re-runnable because tags
            # and upstream commits arrive later than the bump does.
            stats.edges_written = int(
                c.execute(
                    """
                    INSERT INTO dep_bump (
                        consumer_repo_id, consumer_sha, dep_repo_id, dep_name,
                        dep_version, dep_sha, dep_commit_id, manifest,
                        bumped_at, resolution, ecosystem
                    )
                    SELECT DISTINCT ON (t.consumer_repo_id, t.consumer_sha,
                                        t.dep_name, t.dep_version)
                           t.consumer_repo_id, t.consumer_sha, t.dep_repo_id,
                           t.dep_name, t.dep_version, t.dep_sha,
                           dc.id, t.manifest, cc.committed_at,
                           CASE WHEN dc.id IS NOT NULL THEN 'sha' END, t.ecosystem
                    FROM tmp_bump t
                    LEFT JOIN commit cc
                           ON cc.repo_id = t.consumer_repo_id
                          AND cc.sha = t.consumer_sha
                    LEFT JOIN commit dc
                           ON dc.repo_id = t.dep_repo_id
                          AND t.dep_sha IS NOT NULL
                          AND dc.sha LIKE t.dep_sha || '%'
                    ON CONFLICT (consumer_repo_id, consumer_sha, dep_name, dep_version)
                    DO NOTHING
                    """
                ).rowcount
                or 0
            )
            c.execute("DROP TABLE IF EXISTS tmp_bump")

        # Re-run every time: tags and upstream commits arrive on their own
        # schedule, so a bump unresolved last run may be resolvable now.
        resolve_bumps(c)
        stats.resolved_commits = int(
            c.execute("SELECT count(*) FROM dep_bump WHERE dep_commit_id IS NOT NULL")
            .fetchone()[0]
        )
        c.execute(
            "UPDATE repo SET last_depbump_at = now(), last_depbump_sha = head_sha"
            " WHERE id = ANY(%s)",
            ([r[0] for r in targets],),
        )

        stats.duration_s = time.monotonic() - started
        log.info(
            "manifest bumps: scanned %d repos, %d edges found, %d new, "
            "%d resolved to a commit, in %.1fs",
            stats.repos_scanned, stats.edges_found, stats.edges_written,
            stats.resolved_commits, stats.duration_s,
        )
        return stats

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def adoption_delays(limit: int = 20) -> list[dict]:
    """Observed propagation delay per (dependency -> consumer) edge.

    Arithmetic on two known commits: when the upstream change was written, and
    when the consumer took it. Reported, not ranked on -- how long a team takes
    to adopt a release says nothing about whether the dependency is real, which
    the manifest already settled.
    """
    from git_synapse.db.engine import query

    return query(
        """
        SELECT rd.name AS dep, rc.name AS consumer,
               count(*) AS bumps,
               count(*) FILTER (WHERE b.adoption_seconds IS NOT NULL) AS timed,
               round((percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY b.adoption_seconds) / 86400.0)::numeric, 1) AS median_adoption_days,
               round((percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY b.adoption_seconds) / 86400.0)::numeric, 1) AS p90_adoption_days,
               max(b.bumped_at)::date AS last_bump
        FROM dep_bump b
        JOIN repo rc ON rc.id = b.consumer_repo_id
        JOIN repo rd ON rd.id = b.dep_repo_id
        WHERE b.dep_repo_id IS NOT NULL AND b.adoption_seconds >= 0
        GROUP BY 1, 2
        HAVING count(*) >= 3
        ORDER BY bumps DESC
        LIMIT %(limit)s
        """,
        {"limit": limit},
    )
