"""Dependency-bump edges recovered from manifest history.

What this extracts
------------------
A Go pseudo-version embeds the upstream commit it was cut from::

    github.com/acme/signer/v3 v3.0.0-20260626221153-5fc63d6f3055
                                                             ^^^^^^^^^^^^

So a ``go.mod`` diff raising that module is a dated, **directional** statement:
"this consumer commit consumed that signer commit". Unlike the co-occurrence
statistics elsewhere in this package, these rows are ground truth rather than
inference.

Why they exist here
-------------------
Two purposes, in order of importance:

1. **Validation.** They form a labelled set of real propagation edges, which is
   what makes it possible to *measure* whether the directed lagged statistics in
   :mod:`git_synapse.analysis.lagged` actually rank true dependency propagation
   highly, rather than assuming they do.
2. **Empirical lag.** The gap between the upstream commit and the consumer's
   bump is the observed propagation delay, which tells the lagged analysis which
   lags are worth evaluating instead of guessing.

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
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from git_synapse.db.engine import connection, copy_rows
from git_synapse.ingest.gitops import _base_env, mirror_path_for

log = logging.getLogger(__name__)

#: Internal module reference inside a go.mod line, e.g.
#: "github.com/acme/signer/v3 v3.0.0-2026...". The optional /vN major
#: suffix is stripped so the module maps back to a repository name.
_MODULE = re.compile(r"github\.com/acme/([A-Za-z0-9._-]+)(?:/v\d+)?\s+(\S+)")

#: npm dependency on an internal scoped package, as it appears in package.json:
#:     "@acme/console-design-system": "^1.2.3"
#: Covers 75 repositories in this org that go.mod alone would miss entirely.
_NPM = re.compile(r'"@acme/([A-Za-z0-9._-]+)"\s*:\s*"([^"]+)"')

#: A git-URL npm dependency, which is how internal packages are often pinned
#: before they are published:
#:     "console-sdk": "github:acme/console-sdk-js#v1.2.0"
_NPM_GIT = re.compile(
    r'"[^"]+"\s*:\s*"(?:git\+https://github\.com/|github:)acme/'
    r'([A-Za-z0-9._-]+?)(?:\.git)?(?:#([^"]*))?"'
)

#: Manifests scanned, in (filename, ecosystem) form.
MANIFESTS: tuple[tuple[str, str], ...] = (("go.mod", "go"), ("package.json", "npm"))


def _parse_manifest_line(line: str, ecosystem: str) -> tuple[str, str] | None:
    """Extract an internal ``(dep_name, version)`` from one manifest line."""
    if ecosystem == "go":
        match = _MODULE.search(line)
        return (match.group(1), match.group(2)) if match else None

    match = _NPM.search(line)
    if match:
        return match.group(1), match.group(2)
    match = _NPM_GIT.search(line)
    if match:
        return match.group(1), match.group(2) or "git"
    return None

#: Go pseudo-versions come in three shapes, and the separator before the
#: timestamp is '-' in vX.0.0-<ts>-<sha> but '.' in vX.Y.Z-0.<ts>-<sha> and
#: vX.Y.Z-pre.0.<ts>-<sha>. Accepting only '-' silently dropped 17% of edges.
_PSEUDO = re.compile(r"[-.](\d{14})-([0-9a-f]{12})$")

#: Marker prefixing each commit in the log stream. '@@' alone cannot collide
#: with a diff hunk header, which is always followed by a space.
_MARK = "@@"


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


def extract_from_mirror(
    mirror: Path, repo_name: str, manifest: str = "go.mod", ecosystem: str = "go"
) -> list[BumpEdge]:
    """Walk a mirror's manifest history and return every internal bump.

    Only *added* lines are kept: they carry the version being moved **to**, which
    is the one that identifies the upstream commit now being consumed.
    """
    proc = subprocess.run(  # noqa: S603 - fixed executable
        [
            "git", "log", "--all", "--no-merges",
            f"--format={_MARK}%H", "-p", "--unified=0", "--", manifest,
        ],
        cwd=str(mirror),
        env=_base_env(),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=900,
    )
    if proc.returncode != 0:
        log.warning("manifest scan failed for %s: %s", repo_name, proc.stderr[:200])
        return []

    edges: list[BumpEdge] = []
    sha: str | None = None
    for line in proc.stdout.splitlines():
        # A commit marker, not a diff hunk header (which is "@@ -a,b +c,d @@").
        if line.startswith(_MARK) and not line.startswith(_MARK + " "):
            sha = line[len(_MARK):].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if sha is None:
            continue
        parsed = _parse_manifest_line(line, ecosystem)
        if parsed is None:
            continue
        dep_name, version = parsed
        # A module self-reference is the `module` line, not a dependency.
        if dep_name == repo_name:
            continue
        pseudo = _PSEUDO.search(version)
        edges.append(
            BumpEdge(
                consumer_sha=sha,
                dep_name=dep_name,
                dep_version=version,
                dep_sha=pseudo.group(2) if pseudo else None,
                manifest=manifest,
            )
        )
    return edges


def declared_at_head(
    mirror: Path, repo_name: str, manifest: str = "go.mod", ecosystem: str = "go"
) -> list[tuple[str, str]]:
    """Internal dependencies declared in ``manifest`` at HEAD.

    Present-tense and structural, in contrast to :func:`extract_from_mirror`
    which reads history. This is the candidate set for impact prediction.

    Returns:
        ``(dep_name, dep_version)`` pairs, excluding the module's own name.
    """
    proc = subprocess.run(  # noqa: S603 - fixed executable
        ["git", "show", f"HEAD:{manifest}"],
        cwd=str(mirror),
        env=_base_env(),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        return []

    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        # The `module` line names this repo, not a dependency.
        if stripped.startswith("module "):
            continue
        parsed = _parse_manifest_line(line, ecosystem)
        if parsed is None:
            continue
        name, version = parsed
        if name == repo_name:
            continue
        out.setdefault(name, version)
    return sorted(out.items())


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
            " AND (r.last_depbump_sha IS NULL"
            " OR r.head_sha IS NULL"
            " OR r.last_depbump_sha <> r.head_sha)"
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
            {"manifests": [m for m, _ in MANIFESTS]},
        ).fetchall()
        if not rows:
            total = int(c.execute("SELECT count(*) FROM repo_dependency"
                                  " WHERE dep_repo_id IS NOT NULL").fetchone()[0])
            log.info("declared dependencies: no repository manifests changed")
            return total
        name_to_id = {
            row[0]: int(row[1]) for row in c.execute("SELECT name, id FROM repo").fetchall()
        }

        payload = []
        for repo_id, full_name, name in rows:
            mirror = mirror_path_for(full_name)
            if not mirror.is_dir():
                continue
            for manifest, ecosystem in MANIFESTS:
                for dep_name, version in declared_at_head(mirror, name, manifest, ecosystem):
                    payload.append(
                        (repo_id, name_to_id.get(dep_name), dep_name,
                         version[:200], manifest, ecosystem)
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
        {"manifests": [m for m, _ in MANIFESTS]},
    ).fetchall()
    return [(int(r[0]), r[1], r[2]) for r in rows]


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

        # Map repository names to ids once, so a dependency can be resolved to a
        # tracked repo without a query per edge.
        name_to_id = {
            row[0]: int(row[1])
            for row in c.execute("SELECT name, id FROM repo").fetchall()
        }

        payload: list[tuple] = []
        for repo_id, full_name, name in targets:
            mirror = mirror_path_for(full_name)
            if not mirror.is_dir():
                continue
            stats.repos_scanned += 1
            edges: list[BumpEdge] = []
            for manifest, ecosystem in MANIFESTS:
                edges.extend(extract_from_mirror(mirror, name, manifest, ecosystem))
            stats.edges_found += len(edges)
            for edge in edges:
                payload.append(
                    (
                        repo_id,
                        edge.consumer_sha,
                        name_to_id.get(edge.dep_name),
                        edge.dep_name,
                        edge.dep_version[:200],
                        edge.dep_sha,
                        edge.manifest,
                    )
                )

        if payload:
            c.execute(
                """
                CREATE TEMP TABLE tmp_bump (
                    consumer_repo_id BIGINT, consumer_sha TEXT, dep_repo_id BIGINT,
                    dep_name TEXT, dep_version TEXT, dep_sha TEXT, manifest TEXT
                ) ON COMMIT DROP
                """
            )
            copy_rows(
                "tmp_bump",
                ["consumer_repo_id", "consumer_sha", "dep_repo_id", "dep_name",
                 "dep_version", "dep_sha", "manifest"],
                payload,
                conn=c,
            )
            # Resolve the consumer commit for its timestamp, and the dependency
            # commit from the embedded sha. A pseudo-version sha is a 12-char
            # prefix, so the join is a prefix match against the full sha.
            stats.edges_written = int(
                c.execute(
                    """
                    INSERT INTO dep_bump (
                        consumer_repo_id, consumer_sha, dep_repo_id, dep_name,
                        dep_version, dep_sha, dep_commit_id, manifest,
                        bumped_at, lag_seconds
                    )
                    SELECT DISTINCT ON (t.consumer_repo_id, t.consumer_sha,
                                        t.dep_name, t.dep_version)
                           t.consumer_repo_id, t.consumer_sha, t.dep_repo_id,
                           t.dep_name, t.dep_version, t.dep_sha,
                           dc.id, t.manifest, cc.committed_at,
                           CASE WHEN dc.committed_at IS NOT NULL
                                     AND cc.committed_at IS NOT NULL
                                THEN EXTRACT(EPOCH FROM
                                     (cc.committed_at - dc.committed_at))::bigint
                           END
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


def propagation_lags(limit: int = 20) -> list[dict]:
    """Observed propagation delay per (dependency -> consumer) edge.

    The median lag is the empirically correct window for the lagged analysis to
    look in, rather than a guess.
    """
    from git_synapse.db.engine import query

    return query(
        """
        SELECT rd.name AS dep, rc.name AS consumer,
               count(*) AS bumps,
               count(*) FILTER (WHERE b.lag_seconds IS NOT NULL) AS timed,
               round((percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY b.lag_seconds) / 86400.0)::numeric, 1) AS median_lag_days,
               round((percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY b.lag_seconds) / 86400.0)::numeric, 1) AS p90_lag_days,
               max(b.bumped_at)::date AS last_bump
        FROM dep_bump b
        JOIN repo rc ON rc.id = b.consumer_repo_id
        JOIN repo rd ON rd.id = b.dep_repo_id
        WHERE b.dep_repo_id IS NOT NULL AND b.lag_seconds >= 0
        GROUP BY 1, 2
        HAVING count(*) >= 3
        ORDER BY bumps DESC
        LIMIT %(limit)s
        """,
        {"limit": limit},
    )
