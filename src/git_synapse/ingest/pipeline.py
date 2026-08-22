"""End-to-end orchestration: discover -> mirror -> parse -> load -> aggregate -> score.

Repositories are independent of one another at every stage, so the pipeline
fans out across a thread pool. The work is almost entirely I/O -- git talking to
GitHub, and psycopg talking to Postgres -- so threads are the right primitive
despite the GIL; the CPU-bound part (vectorised scoring) releases the GIL inside
numpy anyway.

Failure is isolated per repository. One repo that has been deleted upstream, or
whose history is corrupt, records its error in ``ingest_run_repo`` and leaves
the other 269 to finish. A run reports ``partial`` rather than ``failed`` when
some repos succeeded, because "263 of 270 refreshed" is a materially different
operational situation from "nothing ran".
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from git_synapse.analysis import crossrepo, depbump, lagged, mining, predict
from git_synapse.analysis.aggregate import rebuild_repo
from git_synapse.analysis.score import score_repo
from git_synapse.config import get_config
from git_synapse.db.engine import connection
from git_synapse.ingest import gitops
from git_synapse.ingest.github import GitHubClient, RepoRecord, select_repos
from git_synapse.ingest.parser import iter_commits
from git_synapse.ingest.store import load_commits, upsert_repo

log = logging.getLogger(__name__)


@dataclass
class RepoResult:
    """Outcome of processing a single repository."""

    full_name: str
    repo_id: int | None = None
    status: str = "pending"
    commits_added: int = 0
    files_created: int = 0
    pairs: int = 0
    cloned: bool = False
    blobless: bool = False
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class RunResult:
    """Outcome of a whole pipeline run."""

    run_id: int | None = None
    kind: str = "sync"
    status: str = "running"
    repos: list[RepoResult] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> list[RepoResult]:
        return [r for r in self.repos if r.status == "success"]

    @property
    def failed(self) -> list[RepoResult]:
        return [r for r in self.repos if r.status == "failed"]

    @property
    def commits_added(self) -> int:
        return sum(r.commits_added for r in self.repos)


#: A run still marked "running" after this long was almost certainly killed --
#: its container was stopped, or the host went away. Nothing updates the row in
#: that case, so without reconciliation it blocks every future run forever.
STALE_RUN_HOURS = 6


def reconcile_stale_runs(max_age_hours: int = STALE_RUN_HOURS) -> int:
    """Mark abandoned runs as failed.

    Called on service startup and before any new run is admitted. A run whose
    process died leaves a row stuck in ``running``; the API refuses to start a
    second run while one is in flight, so a single killed container would
    otherwise disable ingestion permanently.

    Returns:
        Number of runs reconciled.
    """
    with connection() as conn:
        count = conn.execute(
            """
            UPDATE ingest_run
               SET status = 'failed',
                   finished_at = now(),
                   duration_s = EXTRACT(EPOCH FROM (now() - started_at)),
                   error = COALESCE(error,
                       'run abandoned: process exited without recording a result')
             WHERE status = 'running'
               AND started_at < now() - make_interval(hours => %s)
            """,
            (max_age_hours,),
        ).rowcount
    if count:
        log.warning("reconciled %d abandoned ingest run(s)", count)
    return int(count or 0)


def active_run() -> dict | None:
    """The currently in-flight run, if one is genuinely still running."""
    reconcile_stale_runs()
    with connection() as conn:
        row = conn.execute(
            """
            SELECT id, kind, trigger, started_at FROM ingest_run
             WHERE status = 'running' ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return {"id": int(row[0]), "kind": row[1], "trigger": row[2], "started_at": row[3]}


def _start_run(kind: str, trigger: str, repos_total: int) -> int:
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO ingest_run (kind, trigger, repos_total) VALUES (%s,%s,%s) RETURNING id",
            (kind, trigger, repos_total),
        ).fetchone()
        return int(row[0])


def _finish_run(run: RunResult) -> None:
    status = "success"
    if run.failed and run.ok:
        status = "partial"
    elif run.failed:
        status = "failed"
    run.status = status

    with connection() as conn:
        conn.execute(
            """
            UPDATE ingest_run SET
                status = %s, finished_at = now(), duration_s = %s,
                repos_ok = %s, repos_failed = %s, commits_added = %s,
                files_added = %s, pairs_written = %s
            WHERE id = %s
            """,
            (
                status,
                run.duration_s,
                len(run.ok),
                len(run.failed),
                run.commits_added,
                sum(r.files_created for r in run.repos),
                sum(r.pairs for r in run.repos),
                run.run_id,
            ),
        )


def _record_repo_result(run_id: int, result: RepoResult) -> None:
    if result.repo_id is None:
        return
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO ingest_run_repo (run_id, repo_id, status, commits_added,
                                         duration_s, error)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (run_id, repo_id) DO UPDATE SET
                status = EXCLUDED.status,
                commits_added = EXCLUDED.commits_added,
                duration_s = EXCLUDED.duration_s,
                error = EXCLUDED.error
            """,
            (
                run_id,
                result.repo_id,
                result.status,
                result.commits_added,
                result.duration_s,
                (result.error or "")[:4000] or None,
            ),
        )


def discover(trigger: str = "manual") -> list[RepoRecord]:
    """Fetch the org's repository list from GitHub and upsert every record.

    Returns the filtered set that ingestion should operate on.
    """
    cfg = get_config().github
    with GitHubClient(cfg) as client:
        records = client.list_org_repos()
    selected = select_repos(records, cfg)

    with connection() as conn:
        for record in selected:
            upsert_repo(record, conn)
    log.info("discovery upserted %d repositories", len(selected))
    return selected


#: Attempts for a repository whose transaction lost a deadlock or serialization
#: race. Postgres resolves a deadlock by killing one participant, and the victim
#: is chosen arbitrarily, so simply trying again is the correct response.
DB_CONTENTION_RETRIES = 3


def sync_repo(record: RepoRecord, force_full: bool = False) -> RepoResult:
    """Mirror, parse and load one repository, retrying on database contention.

    Args:
        record: the repository to process.
        force_full: re-read the entire history even if a watermark exists.

    Returns:
        A :class:`RepoResult`, with ``status='failed'`` and ``error`` set rather
        than raising, so one bad repository cannot abort the run.
    """
    for attempt in range(1, DB_CONTENTION_RETRIES + 1):
        result = _sync_repo_once(record, force_full)
        if result.status != "failed" or not _is_contention(result.error):
            return result
        if attempt < DB_CONTENTION_RETRIES:
            wait = attempt * 2
            log.warning(
                "%s hit database contention (attempt %d/%d); retrying in %ds",
                record.full_name, attempt, DB_CONTENTION_RETRIES, wait,
            )
            time.sleep(wait)
    return result


def _is_contention(error: str | None) -> bool:
    """True if an error string describes a transient database conflict."""
    lowered = (error or "").lower()
    return any(
        marker in lowered
        for marker in ("deadlock", "serializationfailure", "could not serialize")
    )


def _sync_repo_once(record: RepoRecord, force_full: bool = False) -> RepoResult:
    """One attempt at mirroring, parsing, loading, aggregating and scoring."""
    cfg = get_config()
    started = time.monotonic()
    result = RepoResult(full_name=record.full_name)

    try:
        with connection() as conn:
            repo_id = upsert_repo(record, conn)
        result.repo_id = repo_id

        blobless = gitops.choose_clone_mode(record.disk_usage_kb, cfg.ingest)
        result.blobless = blobless

        token = cfg.github.token
        fetch = gitops.sync_mirror(
            record.full_name,
            record.authed_clone_url(token),
            public_url=record.clone_url,
            blobless=blobless,
        )
        result.cloned = fetch.cloned

        with connection() as conn:
            conn.execute(
                """
                UPDATE repo SET mirror_path = %s, head_sha = %s, last_fetch_at = now(),
                                clone_mode = %s, has_churn = %s, mirror_size_kb = %s,
                                ingest_status = 'ingesting', ingest_error = NULL
                WHERE id = %s
                """,
                (
                    str(fetch.path),
                    fetch.head_sha,
                    "blobless" if blobless else "full",
                    not blobless,
                    fetch.size_kb,
                    repo_id,
                ),
            )
            row = conn.execute(
                "SELECT last_ingested_refs, last_ingested_sha FROM repo WHERE id = %s",
                (repo_id,),
            ).fetchone()

        stored_refs: list[str] = []
        if row:
            stored_refs = list(row[0] or [])
            # Fall back to the single-SHA watermark written by older versions.
            if not stored_refs and row[1]:
                stored_refs = [row[1]]

        # A force-push can orphan a previous tip. Asking git for `^<missing>` is
        # a hard error, so drop any SHA the mirror no longer contains rather
        # than failing the whole repository.
        watermarks: list[str] = []
        if not force_full:
            watermarks = [
                sha for sha in stored_refs if gitops.commit_exists(fetch.path, sha)
            ]
            dropped = len(stored_refs) - len(watermarks)
            if dropped:
                log.info(
                    "%s: %d/%d previous ref tips no longer reachable (force-push?)",
                    record.full_name, dropped, len(stored_refs),
                )

        commits = iter_commits(
            fetch.path,
            since_shas=watermarks,
            blobless=blobless,
            reverse=True,
        )

        with connection() as conn:
            stats = load_commits(repo_id, commits, conn)
            # Record every branch tip, so the next run's `git log --all` can
            # exclude all of them and not just the default branch.
            conn.execute(
                """
                UPDATE repo SET last_ingest_at = now(),
                                last_ingested_sha = COALESCE(%s, last_ingested_sha),
                                last_ingested_refs = %s::jsonb
                WHERE id = %s
                """,
                (fetch.head_sha, json.dumps(gitops.ref_tips(fetch.path)), repo_id),
            )

        result.commits_added = stats.commits_written
        result.files_created = stats.files_created

        # Re-derive aggregates only when the atomic data actually moved.
        if stats.commits_written > 0 or force_full:
            with connection() as conn:
                agg = rebuild_repo(repo_id, conn)
                result.pairs = agg.file_pairs
            with connection() as conn:
                score_repo(repo_id, conn)

        with connection() as conn:
            conn.execute(
                "UPDATE repo SET ingest_status='ready', ingest_duration_s=%s WHERE id=%s",
                (time.monotonic() - started, repo_id),
            )

        result.status = "success"

    except Exception as exc:  # noqa: BLE001 - one repo must not kill the run
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("repository %s failed", record.full_name)
        if result.repo_id is not None:
            try:
                with connection() as conn:
                    conn.execute(
                        "UPDATE repo SET ingest_status='failed', ingest_error=%s WHERE id=%s",
                        (result.error[:4000], result.repo_id),
                    )
            except Exception:  # noqa: BLE001 - best effort status write
                log.exception("could not record failure status for %s", record.full_name)

    result.duration_s = time.monotonic() - started
    return result


def run_ingest(
    records: list[RepoRecord] | None = None,
    trigger: str = "manual",
    force_full: bool = False,
    concurrency: int | None = None,
) -> RunResult:
    """Run the full pipeline over a set of repositories.

    Args:
        records: repositories to process. Discovered from GitHub when omitted.
        trigger: ``manual``, ``schedule`` or ``api``; recorded on the run.
        force_full: ignore watermarks and re-read every history.
        concurrency: worker threads; defaults to ``INGEST_CONCURRENCY``.

    Returns:
        A :class:`RunResult` summarising every repository.
    """
    cfg = get_config()
    started = time.monotonic()

    reconcile_stale_runs()

    if records is None:
        records = discover(trigger)

    workers = concurrency or cfg.ingest.concurrency
    run = RunResult(kind="full" if force_full else "sync")
    run.run_id = _start_run(run.kind, trigger, len(records))
    log.info(
        "ingest run %s starting: %d repositories, %d workers",
        run.run_id, len(records), workers,
    )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ingest") as pool:
        futures = {pool.submit(sync_repo, rec, force_full): rec for rec in records}
        done = 0
        for future in as_completed(futures):
            record = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - defensive; sync_repo catches
                result = RepoResult(
                    full_name=record.full_name,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            run.repos.append(result)
            _record_repo_result(run.run_id, result)
            done += 1
            log.info(
                "[%d/%d] %-55s %-8s %5d commits %6.1fs%s",
                done, len(records), result.full_name, result.status,
                result.commits_added, result.duration_s,
                " (blobless)" if result.blobless else "",
            )

    # Cross-repo coupling is inherently global -- a change set spans
    # repositories -- so it runs once after all per-repo work completes, rather
    # than inside the per-repo fan-out. Skipped when nothing changed, and a
    # failure here must not fail the whole run: the per-repo results are already
    # committed and useful on their own.
    if cfg.crossrepo.enabled and (run.commits_added > 0 or force_full):
        # Each stage is independent and guarded separately: a failure in one
        # must not discard the others, and none of them can invalidate the
        # per-repo results that are already committed.
        try:
            xr = crossrepo.rebuild(force=force_full)
            log.info(
                "cross-repo: %d change sets, %d repo pairs, %d file pairs in %.1fs",
                xr.change_sets, xr.repo_pairs, xr.file_pairs, xr.duration_s,
            )
        except Exception:  # noqa: BLE001 - per-repo results stay valid
            log.exception("cross-repo rebuild failed; per-repo data is unaffected")

        # Manifest bumps first: they are incremental per repository, and the
        # propagation lags they measure are what justify the lag windows below.
        try:
            db = depbump.rebuild(force=force_full)
            log.info(
                "manifest bumps: %d repos scanned, %d new edges in %.1fs",
                db.repos_scanned, db.edges_written, db.duration_s,
            )
        except Exception:  # noqa: BLE001
            log.exception("manifest bump scan failed")

        # Directed lagged coupling is a full rebuild rather than a delta: it is
        # a few seconds of matrix arithmetic over the whole corpus, so an
        # incremental variant would add complexity for no measurable gain.
        try:
            depbump.refresh_declared(force=force_full)
        except Exception:  # noqa: BLE001
            log.exception("declared dependency refresh failed")

        try:
            lg = lagged.rebuild(force=force_full)
            log.info(
                "lagged coupling: %d rows over %d bins in %.1fs",
                lg.rows_written, lg.n_bins, lg.duration_s,
            )
        except Exception:  # noqa: BLE001
            log.exception("lagged coupling rebuild failed")

        # Impact prediction depends on all three of the above, so it runs last.
        try:
            pr = predict.rebuild(force=force_full)
            log.info(
                "impact: %d edges across %d repos in %.1fs",
                pr.rows_written, pr.sources, pr.duration_s,
            )
        except Exception:  # noqa: BLE001
            log.exception("impact prediction failed")

        try:
            mn = mining.rebuild(force=force_full)
            log.info(
                "mining: %d modules, %d drift rows, %d risk rows in %.1fs",
                mn.clusters, mn.drift_rows, mn.risk_rows, mn.duration_s,
            )
        except Exception:  # noqa: BLE001
            log.exception("mining rebuild failed")

    run.duration_s = time.monotonic() - started
    _finish_run(run)
    log.info(
        "ingest run %s finished in %.1fs: %d ok, %d failed, %d commits added",
        run.run_id, run.duration_s, len(run.ok), len(run.failed), run.commits_added,
    )
    return run


def load_repo_records() -> list[RepoRecord]:
    """Rebuild :class:`RepoRecord` objects from the database.

    Lets a refresh run without calling the GitHub API, which is useful when
    re-processing after a config change or when the API is rate limited.
    """
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT github_id, owner, name, full_name, clone_url, default_branch,
                   disk_usage_kb, is_private, is_fork, is_archived
            FROM repo WHERE is_enabled ORDER BY id
            """
        ).fetchall()

    return [
        RepoRecord(
            github_id=r[0] or 0,
            owner=r[1],
            name=r[2],
            full_name=r[3],
            clone_url=r[4],
            default_branch=r[5],
            disk_usage_kb=r[6],
            is_private=bool(r[7]),
            is_fork=bool(r[8]),
            is_archived=bool(r[9]),
        )
        for r in rows
    ]
