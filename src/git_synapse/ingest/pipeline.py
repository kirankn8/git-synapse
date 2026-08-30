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

import contextlib
import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from git_synapse.analysis import depbump, mining, predict
from git_synapse.analysis.aggregate import rebuild_repo
from git_synapse.analysis.score import score_repo
from git_synapse.config import get_config
from git_synapse.db.engine import connection, copy_rows
from git_synapse.ingest import accounts, gitops
from git_synapse.ingest.github import GitHubClient, RepoRecord, select_repos
from git_synapse.ingest.parser import iter_commits
from git_synapse.ingest.store import load_commits, load_tags, upsert_repo

log = logging.getLogger(__name__)

#: Consecutive network-failed repositories before a run gives up. Set above the
#: worker count so a single unlucky burst cannot trip it.
NETWORK_FAILURE_ABORT = 12

#: A discovery returning less than this fraction of the repositories already
#: known is treated as a failed listing rather than as the org having shrunk.
DISCOVERY_SHRINK_FLOOR = 0.8

#: Advisory lock key serialising ingest runs across processes. Arbitrary but
#: fixed; anything else taking this key would deadlock with the pipeline.
INGEST_LOCK_KEY = 0x0C047E5


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

    Liveness is the ingest advisory lock rather than the row's age: a run holds
    it for its whole life, so its absence is proof the run is gone. The age
    window remains as a backstop.

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
               AND (
                     started_at < now() - make_interval(hours => %s)
                     -- A live run holds the ingest advisory lock for as long as
                     -- it runs, so a `running` row with no lock behind it is
                     -- provably dead. Waiting out the age window instead left a
                     -- crashed run blocking the API's refresh endpoint for six
                     -- hours while the scheduler carried on regardless.
                     OR NOT EXISTS (
                         SELECT 1 FROM pg_locks
                          WHERE locktype = 'advisory'
                            AND objid = %s
                     )
                   )
            """,
            (max_age_hours, INGEST_LOCK_KEY & 0xFFFFFFFF),
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


class AuthError(RuntimeError):
    """The GitHub credential is missing or rejected."""


def private_repos_in_scope() -> int:
    """How many repositories about to be mirrored are private.

    A token is only genuinely required for those. An entirely public corpus --
    a public organisation, or an allowlist of public repositories -- clones over
    plain HTTPS and needs no credential at all.
    """
    with connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM repo WHERE is_enabled AND is_private"
        ).fetchone()
    return int(row[0] or 0)


def verify_credentials(required: bool = True) -> str:
    """Confirm the token works before any mirror is touched.

    Called at the start of every run. Without it, an expired token produces 272
    individually-failing repositories and a run that looks like a mass outage
    instead of one bad credential -- and `gh auth token` yields short-lived
    `ghu_` tokens, so expiry is routine rather than exceptional.

    Returns:
        The authenticated login.

    Raises:
        AuthError: if the token is absent or rejected.
    """
    cfg = get_config().github
    token = cfg.current_token()
    if not token:
        if not required:
            log.info("no GITHUB_TOKEN; every repository in scope is public, "
                     "so cloning proceeds unauthenticated")
            return "anonymous"
        raise AuthError(
            "GITHUB_TOKEN is empty, and private repositories are in scope. "
            "Set it in .env and restart the affected services."
        )

    import httpx

    try:
        response = httpx.get(
            f"{cfg.api_url}/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        # A network problem is not an auth problem; let the run proceed and let
        # the per-repo retry logic deal with it.
        log.warning("could not reach the GitHub API to verify the token: %s", exc)
        return "unverified"

    if response.status_code == 401:
        raise AuthError(
            "GITHUB_TOKEN was rejected (HTTP 401). It has most likely expired -- "
            "`gh auth token` issues short-lived credentials. The daemon keeps "
            "the mounted token file current; if it is not running, refresh it "
            "with scripts/refresh-token.sh, "
            "then `docker compose up -d`. No mirrors were touched."
        )
    if response.status_code >= 400:
        raise AuthError(
            f"GitHub rejected the token with HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    login = (response.json() or {}).get("login", "unknown")
    log.info("github credential verified as %s", login)
    return login


def discover(trigger: str = "manual") -> list[RepoRecord]:
    """List every configured account's repositories and upsert every record.

    Returns the filtered set that ingestion should operate on.
    """
    accounts.seed_from_env()
    configured = accounts.list_accounts(enabled_only=True)
    if not configured:
        raise AuthError(
            "no accounts are configured. Add an organisation or user to scan "
            "on the Accounts page, or with `git-synapse account add <login>`."
        )

    selected: list[RepoRecord] = []
    owners: dict[str, int] = {}
    failures: list[str] = []
    #: Repositories the API listed, before this account's filters were applied.
    listed = 0
    for account in configured:
        try:
            found, raw = _discover_account(account)
        except AuthError:
            # A bad credential is not this account's fault and retrying the
            # rest would repeat the same failure against every one of them.
            raise
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
            log.warning("discovery failed for %s: %s", account["login"], exc)
            accounts.record_discovery(account["id"], 0, str(exc))
            failures.append(f"{account['login']}: {exc}")
            continue
        listed += raw
        accounts.record_discovery(account["id"], len(found))
        for record in found:
            owners[record.full_name] = account["id"]
        selected.extend(found)

    if not selected and failures:
        raise AuthError("every configured account failed discovery: " + "; ".join(failures))

    # A discovery that collapses is a symptom, not a fact about the accounts: an
    # unauthenticated request returns only public repositories, with HTTP 200 and
    # no error, and the run then quietly refreshes a fraction of the corpus.
    #
    # The comparison is against what the API *listed*, not what survived the
    # filters. Measuring the filtered count made narrowing an allowlist
    # indistinguishable from a broken credential, and refused the configuration
    # change with an error about the credential. Skipped when an account errored,
    # since then the shrinkage is explained and already reported.
    with connection() as conn:
        known = int(conn.execute("SELECT count(*) FROM repo WHERE is_enabled").fetchone()[0])
    if not failures and known and listed < known * DISCOVERY_SHRINK_FLOOR:
        raise AuthError(
            f"the API listed {listed} repositories but {known} are already known. "
            "That is the shape of an unauthenticated or partial listing, not "
            "repositories disappearing. Check the credential; nothing was changed."
        )

    with connection() as conn:
        for record in selected:
            upsert_repo(record, conn, account_id=owners.get(record.full_name))
    log.info("discovery upserted %d repositories from %d accounts", len(selected), len(configured))
    return selected


def _discover_account(account: dict) -> tuple[list[RepoRecord], int]:
    """List and filter one account, returning the kept records and the raw count.

    The raw count is what the shrink guard has to reason about: narrowing an
    allowlist legitimately collapses the *filtered* result, while a credential
    that has stopped working collapses the *listing*.
    """
    cfg = accounts.config_for(account)
    with GitHubClient(cfg) as client:
        records = client.list_account_repos(account["login"], account["kind"])
    return select_repos(records, cfg), len(records)


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


def _drop_unreachable_commits(repo_id: int, mirror: Path) -> int:
    """Delete commits the mirror no longer reaches, e.g. after a force-push.

    Mirrors are pruned on fetch but the database was insert-only, so rewritten
    history accumulated forever: 735 commits across 24 repositories, inflating
    the ``N`` of every contingency table in those repos and keeping files alive
    that were deliberately removed. The reachable-set walk is only worth its cost
    when the counts actually disagree, so a cheap comparison gates it.

    Reachability is measured from the default branch, matching what the ingest
    walks. A commit that only ever lived on a branch that never merged is not
    part of the shipped history and must not be counted as one.
    """
    with connection() as conn:
        stored = int(
            conn.execute(
                "SELECT count(*) FROM commit WHERE repo_id = %s", (repo_id,)
            ).fetchone()[0]
        )
    if stored == 0:
        return 0
    try:
        proc = subprocess.run(  # noqa: S603 - fixed executable
            ["git", "rev-list", "HEAD", "--no-merges", "--count"],
            cwd=str(mirror), capture_output=True, text=True, timeout=600, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode != 0 or stored <= int(proc.stdout.strip() or 0):
        return 0

    try:
        walk = subprocess.run(  # noqa: S603 - fixed executable
            ["git", "rev-list", "HEAD", "--no-merges"],
            cwd=str(mirror), capture_output=True, text=True, timeout=900, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if walk.returncode != 0:
        return 0
    reachable = [line for line in walk.stdout.split("\n") if line]
    if not reachable:
        return 0

    with connection() as conn:
        conn.execute(
            "CREATE TEMP TABLE reachable_sha (sha TEXT PRIMARY KEY) ON COMMIT DROP"
        )
        copy_rows("reachable_sha", ["sha"], ((sha,) for sha in reachable), conn=conn)
        return int(
            conn.execute(
                """
                DELETE FROM commit c
                WHERE c.repo_id = %s
                  AND NOT EXISTS (SELECT 1 FROM reachable_sha r WHERE r.sha = c.sha)
                """,
                (repo_id,),
            ).rowcount
            or 0
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

        token = cfg.github.current_token()
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
            # After the commits, so each tag resolves to a row rather than
            # leaving commit_id null on the first run.
            load_tags(repo_id, gitops.read_tags(gitops.mirror_path_for(record.full_name)), conn)
            # Record the default branch tip as the next run's exclusion point.
            # This was every branch tip when the walk covered every branch;
            # excluding more than the walk visits would skip commits that must
            # still be read when their branch merges.
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

        # History that has been rewritten away still counts toward N.
        removed = _drop_unreachable_commits(repo_id, fetch.path)
        if removed:
            log.info("repo %d: removed %d commit(s) no longer in git", repo_id, removed)

        # Re-derive aggregates when the atomic data moved, or when a previous
        # pass ingested commits but failed before aggregating them. Without the
        # second condition that failure was permanent: the commit watermark had
        # already advanced, so every later run saw nothing to do.
        with connection() as conn:
            stale_aggregate = bool(
                conn.execute(
                    "SELECT last_aggregate_sha IS DISTINCT FROM last_ingested_sha"
                    " FROM repo WHERE id = %s",
                    (repo_id,),
                ).fetchone()[0]
            )
        if stats.commits_written > 0 or removed or force_full or stale_aggregate:
            with connection() as conn:
                agg = rebuild_repo(repo_id, conn)
                result.pairs = agg.file_pairs
            with connection() as conn:
                score_repo(repo_id, conn)
            # Only now is the repository's derived state actually current.
            with connection() as conn:
                conn.execute(
                    "UPDATE repo SET last_aggregate_sha = last_ingested_sha"
                    " WHERE id = %s",
                    (repo_id,),
                )

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

    # One ingest at a time, across processes. A scheduled tick and a human's
    # `git-synapse ingest` used to run concurrently: they fetched the same mirrors,
    # redid the same global rebuilds, and left rows stuck in `running` that
    # blocked the API refresh endpoint for six hours. An advisory lock is held
    # for the life of the connection, so a crashed run releases it immediately
    # rather than wedging the next one.
    with connection() as conn:
        if not conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (INGEST_LOCK_KEY,)
        ).fetchone()[0]:
            log.warning("another ingest run holds the lock; skipping this one")
            run = RunResult(kind="full" if force_full else "sync")
            run.status = "skipped"
            run.duration_s = time.monotonic() - started
            return run

        try:
            return _run_ingest_locked(
                records, trigger, force_full, concurrency, started
            )
        finally:
            # An advisory lock outlives the transaction and is released only by
            # unlocking or by the session ending -- and a pooled connection's
            # session does not end when it is returned. So the unlock has to
            # happen, but it must not raise: a failing one would mask whatever
            # actually went wrong, and the pool discards a broken connection,
            # which ends its session and releases the lock anyway.
            with contextlib.suppress(Exception):
                conn.execute("SELECT pg_advisory_unlock(%s)", (INGEST_LOCK_KEY,))


def _run_ingest_locked(
    records: list[RepoRecord] | None,
    trigger: str,
    force_full: bool,
    concurrency: int | None,
    started: float,
) -> RunResult:
    """The body of :func:`run_ingest`, with the single-run lock already held."""
    cfg = get_config()

    reconcile_stale_runs()

    # Fail the whole run on a bad credential rather than letting every
    # repository fail individually. Deliberately before any mirror is touched.
    #
    # Required only when something private is in scope: refusing to run at all
    # on a wholly public corpus blocked a legitimate configuration for no
    # reason, since those clone over plain HTTPS.
    try:
        verify_credentials(required=private_repos_in_scope() > 0)
    except AuthError as exc:
        log.error("aborting run: %s", exc)
        run = RunResult(kind="full" if force_full else "sync")
        run.run_id = _start_run(run.kind, trigger, 0)
        run.duration_s = time.monotonic() - started
        run.status = "failed"
        with connection() as conn:
            conn.execute(
                "UPDATE ingest_run SET status='failed', finished_at=now(),"
                " duration_s=%s, error=%s WHERE id=%s",
                (run.duration_s, str(exc)[:4000], run.run_id),
            )
        return run

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
        consecutive_network_failures = 0
        aborted = False
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

            # When connectivity goes, every repository fails the same way after
            # exhausting its retries -- four attempts at a two-minute timeout is
            # nine minutes each. Grinding through the whole corpus that way took
            # 25 minutes to accomplish nothing. Give up once the pattern is
            # unmistakable; the mirrors are untouched and the next run retries.
            if result.status == "failed" and gitops.is_transient_error(
                result.error or ""
            ):
                consecutive_network_failures += 1
            elif result.status != "failed":
                consecutive_network_failures = 0

            if consecutive_network_failures >= NETWORK_FAILURE_ABORT and not aborted:
                aborted = True
                log.error(
                    "aborting run: %d consecutive repositories failed with network "
                    "errors, so the network is down rather than the repositories. "
                    "Mirrors are untouched; the next run will retry.",
                    consecutive_network_failures,
                )
                for pending in futures:
                    pending.cancel()

            run.repos.append(result)
            _record_repo_result(run.run_id, result)
            done += 1
            log.info(
                "[%d/%d] %-55s %-8s %5d commits %6.1fs%s",
                done, len(records), result.full_name, result.status,
                result.commits_added, result.duration_s,
                " (blobless)" if result.blobless else "",
            )

    # The dependency graph is global -- an edge spans repositories -- so it runs
    # once after all per-repo work completes. A failure here must not fail the
    # whole run: the per-repo results are already committed and useful alone.
    if cfg.crossrepo.enabled and (run.commits_added > 0 or force_full):
        # Manifest bumps first: they are incremental per repository, and they
        # are what dates every edge the graph below carries.
        try:
            db = depbump.rebuild(force=force_full)
            log.info(
                "manifest bumps: %d repos scanned, %d new edges in %.1fs",
                db.repos_scanned, db.edges_written, db.duration_s,
            )
        except Exception:  # noqa: BLE001
            log.exception("manifest bump scan failed")

        try:
            depbump.refresh_declared(force=force_full)
            depbump.refresh_modules()
        except Exception:  # noqa: BLE001
            log.exception("declared dependency refresh failed")

        # Impact ranks the declared graph, so it runs after both stages above.
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

    Every descriptive column is read back, not just the handful the pipeline
    needs. The record is written straight back out by ``upsert_repo``, so a
    partial read here silently blanked everything it omitted: language,
    description, topics, licence and stars were wiped on every ingest.
    """
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT github_id, owner, name, full_name, clone_url, default_branch,
                   disk_usage_kb, is_private, is_fork, is_archived,
                   description, homepage, html_url, ssh_url, primary_language,
                   topics, license_spdx, visibility, is_template, is_disabled,
                   stargazers, watchers, forks_count, open_issues,
                   github_created_at, github_updated_at, github_pushed_at
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
            description=r[10],
            homepage=r[11],
            html_url=r[12],
            ssh_url=r[13],
            primary_language=r[14],
            topics=list(r[15] or []),
            license_spdx=r[16],
            visibility=r[17],
            is_template=bool(r[18]),
            is_disabled=bool(r[19]),
            stargazers=r[20] or 0,
            watchers=r[21] or 0,
            forks_count=r[22] or 0,
            open_issues=r[23] or 0,
            github_created_at=r[24],
            github_updated_at=r[25],
            github_pushed_at=r[26],
        )
        for r in rows
    ]
