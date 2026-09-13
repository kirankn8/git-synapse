"""End-to-end orchestration: discover -> mirror -> parse -> load -> aggregate -> score."""

from __future__ import annotations

import dataclasses
import logging
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import DBAPIError, OperationalError

from git_synapse.analysis import depbump, derived, mining, predict
from git_synapse.analysis.aggregate import rebuild_repo
from git_synapse.analysis.score import score_repo
from git_synapse.config import get_config
from git_synapse.db.engine import SCHEMA_VERSION, schema_drift
from git_synapse.db.orm import models, session_scope
from git_synapse.ingest import accounts, gitops, providers, sources
from git_synapse.ingest.github import RepoRecord, select_repos
from git_synapse.ingest.parser import iter_commits
from git_synapse.ingest.store import load_commits, load_tags, upsert_repo


def _mark_replays(repo_id: int, shas: set[str], conn: object) -> int:
    """Flag commits whose change already exists on the shipping branch."""
    if not shas:
        return 0
    Commit = models().Commit
    return int(
        conn.query(Commit)
        .filter(Commit.repo_id == repo_id, Commit.sha.in_(shas), Commit.is_replay.is_(False))
        .update({Commit.is_replay: True, Commit.pair_eligible: False}, synchronize_session=False)
        or 0
    )

log = logging.getLogger(__name__)

NETWORK_FAILURE_ABORT = 12

DISCOVERY_SHRINK_FLOOR = 0.8



def _try_ingest_lock(session: object) -> bool:
    """Acquire a transaction-scoped ORM row lock for the ingest run."""
    Meta = models().Meta
    try:
        row = session.get(Meta, "lock:ingest", with_for_update={"nowait": True})
        if row is None:
            session.add(Meta(key="lock:ingest", value={"owner": "ingest"}))
            session.flush()
        return True
    except (OperationalError, DBAPIError) as exc:
        if getattr(getattr(exc, "orig", None), "args", None):
            detail = str(exc.orig.args[0])
            # 55P03 is lock_not_available: another run holds the ingest lock.
            if "55P03" not in detail and "could not obtain lock" not in detail:
                raise
        session.rollback()
        return False


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


STALE_RUN_HOURS = 6


def reconcile_stale_runs(max_age_hours: int = STALE_RUN_HOURS) -> int:
    """Mark abandoned runs as failed."""
    cutoff = datetime.now(UTC).timestamp() - max_age_hours * 3600
    cutoff_at = datetime.fromtimestamp(cutoff, tz=UTC)
    IngestRun = models().IngestRun
    with session_scope() as conn:
        rows = conn.query(IngestRun).filter(
            IngestRun.status == "running", IngestRun.started_at < cutoff_at,
        ).all()
        now = datetime.now(UTC)
        for row in rows:
            row.status = "failed"
            row.finished_at = now
            row.duration_s = max(0.0, (now - row.started_at).total_seconds())
            row.error = row.error or "run abandoned: process exited without recording a result"
        count = len(rows)
    if count:
        log.warning("reconciled %d abandoned ingest run(s)", count)
    return int(count or 0)


def active_run() -> dict | None:
    """The currently in-flight run, if one is genuinely still running."""
    reconcile_stale_runs()
    IngestRun = models().IngestRun
    with session_scope() as conn:
        row = conn.query(IngestRun).filter(IngestRun.status == "running").order_by(
            IngestRun.started_at.desc()
        ).first()
    if row is None:
        return None
    return {"id": int(row.id), "kind": row.kind, "trigger": row.trigger, "started_at": row.started_at}


def _start_run(kind: str, trigger: str, repos_total: int) -> int:
    IngestRun = models().IngestRun
    with session_scope() as conn:
        row = IngestRun(kind=kind, trigger=trigger, repos_total=repos_total)
        conn.add(row)
        conn.flush()
        return int(row.id)


def _prune_call_log() -> None:
    """Trim the call log at the end of a run."""
    from git_synapse.analysis import calls

    try:
        removed = calls.prune()
        if removed:
            log.info("call log: pruned %d row(s)", removed)
    except Exception:
        log.warning("could not prune the call log", exc_info=True)

    try:
        from git_synapse import auth

        gone = auth.prune_sessions()
        if gone:
            log.info("sessions: pruned %d expired", gone)
        stale = auth.prune_login_attempts()
        if stale:
            log.info("login attempts: pruned %d past the window", stale)
    except Exception:
        log.warning("could not prune expired sessions", exc_info=True)


def _failure_summary(run: RunResult) -> str | None:
    """One sentence explaining a failed run, or None when nothing failed."""
    if not run.failed:
        return None

    counts = Counter((r.error or "unknown error").strip().splitlines()[0][:300]
                     for r in run.failed)
    top, n = counts.most_common(1)[0]
    lead = f"{len(run.failed)} of {len(run.repos)} repositories failed"
    if len(counts) == 1:
        return f"{lead}, every one with: {top}"
    others = len(counts) - 1
    return (f"{lead}; the most common ({n}) was: {top}"
            f" \u2014 and {others} other kind{'s' if others > 1 else ''} of error")


def _crossrepo_rebuild_needed() -> bool:
    """Return whether a previous cross-repository stage is incomplete."""
    Repo = models().Repo
    File = models().File
    Meta = models().Meta
    with session_scope() as conn:
        manifest_names = set(depbump.manifests.MANIFEST_FILES)
        repos = conn.query(Repo).filter(Repo.is_enabled.is_(True)).all()
        manifest_repo_ids = {
            row.repo_id for row in conn.query(File.repo_id, File.basename).filter(
                File.basename.in_(manifest_names)
            ).all()
        }
        stale = any(
            ((repo.id in manifest_repo_ids
              and (repo.last_depbump_sha != repo.head_sha or repo.last_depbump_sha is None
                   or repo.last_declared_sha != repo.head_sha or repo.last_declared_sha is None))
             or (repo.pair_count > 0 and (
                 repo.last_mining_at is None or repo.last_aggregate_at is None
                 or repo.last_mining_at < repo.last_aggregate_at
             )))
            for repo in repos
        )
        if stale:
            return True
        fingerprint = predict._input_fingerprint(conn)
        stored = conn.get(Meta, "watermark:predict_inputs")
        return stored is None or stored.value != fingerprint


def _finish_run(run: RunResult) -> None:
    status = "success"
    if run.failed and run.ok:
        status = "partial"
    elif run.failed:
        status = "failed"
    run.status = status

    IngestRun = models().IngestRun
    with session_scope() as conn:
        row = conn.get(IngestRun, run.run_id)
        if row is not None:
            row.status = status
            row.finished_at = datetime.now(UTC)
            row.duration_s = run.duration_s
            row.repos_ok = len(run.ok)
            row.repos_failed = len(run.failed)
            row.commits_added = run.commits_added
            row.files_added = sum(r.files_created for r in run.repos)
            row.pairs_written = sum(r.pairs for r in run.repos)
            row.error = _failure_summary(run)


def _record_repo_result(run_id: int, result: RepoResult) -> None:
    if result.repo_id is None:
        return
    IngestRunRepo = models().IngestRunRepo
    with session_scope() as conn:
        row = conn.get(IngestRunRepo, (run_id, result.repo_id))
        if row is None:
            row = IngestRunRepo(run_id=run_id, repo_id=result.repo_id)
            conn.add(row)
        row.status = result.status
        row.commits_added = result.commits_added
        row.duration_s = result.duration_s
        row.error = (result.error or "")[:4000] or None


class AuthError(RuntimeError):
    """The GitHub credential is missing or rejected."""


def private_repos_in_scope() -> int:
    """How many repositories about to be mirrored are private."""
    Repo = models().Repo
    with session_scope() as conn:
        return int(conn.query(Repo).filter(Repo.is_enabled.is_(True), Repo.is_private.is_(True)).count())


def verify_credentials(required: bool = True) -> str:
    """Confirm the token works before any mirror is touched."""
    cfg = get_config().providers.github
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
    """List every configured account's repositories and upsert every record."""
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
            raise
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
            log.warning("discovery failed for %s: %s", account["login"], exc)
            accounts.record_discovery(account["id"], error=str(exc))
            failures.append(f"{account['login']}: {exc}")
            continue
        listed += raw
        accounts.record_discovery(account["id"], len(found))
        for record in found:
            owners[record.full_name] = account["id"]
        selected.extend(found)

    if not selected and failures:
        raise AuthError("every configured account failed discovery: " + "; ".join(failures))

    Repo = models().Repo
    with session_scope() as conn:
        known = int(conn.query(Repo).filter(Repo.is_enabled.is_(True)).count())
    if not failures and known and listed < known * DISCOVERY_SHRINK_FLOOR:
        raise AuthError(
            f"the API listed {listed} repositories but {known} are already known. "
            "That is the shape of an unauthenticated or partial listing, not "
            "repositories disappearing. Check the credential; nothing was changed."
        )

    with session_scope() as conn:
        for record in selected:
            upsert_repo(record, conn, account_id=owners.get(record.full_name))
    accounts.refresh_repo_counts()
    log.info("discovery upserted %d repositories from %d accounts", len(selected), len(configured))
    return selected


def _discover_account(account: dict) -> tuple[list[RepoRecord], int]:
    """List and filter one source, returning the kept records and the raw count."""
    cfg = accounts.config_for(account)
    host = account.get("host") or "github.com"
    source = sources.parse(f"https://{host}/{account['login']}")
    overrides = {}
    if account.get("provider"):
        overrides["provider"] = account["provider"]
    if account.get("api_url"):
        # A self-hosted install: the account knows the endpoint, the host does not.
        overrides["api_url"] = account["api_url"]
    if overrides:
        source = dataclasses.replace(source, **overrides)

    login = account["login"]
    only = list(account.get("only_repos") or ())
    token = accounts.credential_for(accounts._with_credential(account))

    with providers.for_source(source, token=token, patient=False) as client:
        if not client.supports_listing():
            if not only:
                log.warning("%s has no API to enumerate; add its repositories by URL",
                            login)
                return [], 0
            return _fetch_by_name(client, login, only), len(only)

        first = client.list_page(login, 1)
        if not first.has_more:
            records = first.records
        else:
            remaining_pages = (
                (first.total - len(first.records) + providers.PAGE - 1) // providers.PAGE
                if first.total else None
            )
            if only and (remaining_pages is None or remaining_pages > len(only)):
                return _fetch_by_name(client, login, only), len(only)
            records = list(first.records)
            page = 1
            while True:
                page += 1
                nxt = client.list_page(login, page)
                records.extend(nxt.records)
                if not nxt.has_more:
                    break

        if not only:
            for record in records:
                if record.is_fork and not record.parent_full_name:
                    record.parent_full_name = client.fetch_parent(record.full_name)

    if only:
        wanted = {n.lower().strip("/") for n in only}
        # Match under the owner: on GitLab a bare name also matches nested groups.
        prefix = f"{login.lower()}/"
        kept = [r for r in records
                if r.full_name.lower() in wanted
                or r.full_name.lower().removeprefix(prefix) in wanted]
        return kept, len(records)

    return select_repos(records, cfg, tracked=_tracked_full_names()), len(records)


def _tracked_full_names() -> frozenset[str]:
    """Every repository already in the corpus, lowercased."""
    Repo = models().Repo
    with session_scope() as conn:
        rows = conn.query(Repo.full_name).filter(Repo.is_enabled.is_(True)).all()
    return frozenset(row.full_name.lower() for row in rows if row.full_name)


def _fetch_by_name(client, login: str, names: list[str]) -> list[RepoRecord]:
    """Fetch exactly the repositories an allowlist names, one request each."""
    out: list[RepoRecord] = []
    for name in names:
        try:
            out.append(client.get_repo(login, name))
        except Exception as exc:  # noqa: BLE001 - recorded, then carry on
            log.warning("could not fetch %s/%s: %s", login, name, exc)
    return out


DB_CONTENTION_RETRIES = 3


def sync_repo(record: RepoRecord, force_full: bool = False) -> RepoResult:
    """Mirror, parse and load one repository, retrying on database contention."""
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
    """Delete commits the mirror no longer reaches, e.g. after a force-push."""
    Commit = models().Commit
    with session_scope() as conn:
        stored = int(conn.query(Commit).filter(Commit.repo_id == repo_id).count())
    if stored == 0:
        return 0
    try:
        proc = subprocess.run(
            ["git", "rev-list", "HEAD", "--tags", "--no-merges", "--count"],
            cwd=str(mirror), capture_output=True, text=True, timeout=600, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode != 0 or stored <= int(proc.stdout.strip() or 0):
        return 0

    try:
        walk = subprocess.run(
            ["git", "rev-list", "HEAD", "--tags", "--no-merges"],
            cwd=str(mirror), capture_output=True, text=True, timeout=900, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if walk.returncode != 0:
        return 0
    reachable = [line for line in walk.stdout.split("\n") if line]
    if not reachable:
        return 0

    with session_scope() as conn:
        doomed = conn.query(Commit).filter(
            Commit.repo_id == repo_id, ~Commit.sha.in_(reachable)
        ).all()
        count = len(doomed)
        for row in doomed:
            conn.delete(row)
        return count


def _clone_token(record: RepoRecord) -> str:
    """The access token for this repository's source, if it carries one."""
    try:
        account = accounts.find_by_login(record.owner, record.host)
        return accounts.credential_for(accounts._with_credential(account))
    except Exception:  # noqa: BLE001 - an unreadable credential must not stop
        # the ingest; the deployment-wide token is a working fallback.
        return ""


def _sync_repo_once(record: RepoRecord, force_full: bool = False) -> RepoResult:
    """One attempt at mirroring, parsing, loading, aggregating and scoring."""
    cfg = get_config()
    started = time.monotonic()
    result = RepoResult(full_name=record.full_name)

    try:
        with session_scope() as conn:
            repo_id = upsert_repo(record, conn)
        result.repo_id = repo_id

        blobless = gitops.choose_clone_mode(record.disk_usage_kb, cfg.ingest)
        result.blobless = blobless

        token = _clone_token(record) or cfg.providers.github.current_token()
        fetch = gitops.sync_mirror(
            record.full_name,
            record.authed_clone_url(token),
            public_url=record.clone_url,
            blobless=blobless,
            host=record.host,
        )
        result.cloned = fetch.cloned

        Repo = models().Repo
        with session_scope() as conn:
            repo_row = conn.get(Repo, repo_id)
            if repo_row is None:
                raise RuntimeError(f"repository {repo_id} disappeared during ingest")
            repo_row.mirror_path = str(fetch.path)
            repo_row.head_sha = fetch.head_sha
            repo_row.last_fetch_at = datetime.now(UTC)
            repo_row.clone_mode = "blobless" if blobless else "full"
            repo_row.has_churn = not blobless
            repo_row.mirror_size_kb = fetch.size_kb
            repo_row.ingest_status = "ingesting"
            repo_row.ingest_error = None
            stored_refs = list(repo_row.last_ingested_refs or [])
            stored_sha = repo_row.last_ingested_sha

        if not stored_refs and stored_sha:
            stored_refs = [stored_sha]

        watermarks: list[str] = []
        if not force_full:
            # A force-push can orphan an old tip, and ^<missing-sha> is a hard git error.
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
            include_tags=True,
        )

        with session_scope() as conn:
            stats = load_commits(repo_id, commits, conn)
            branch = gitops.default_branch(fetch.path)
            _mark_replays(repo_id, gitops.replayed_commits(fetch.path, branch), conn)
            mirror = gitops.mirror_path_for(record.full_name, host=record.host)
            load_tags(repo_id, gitops.read_tags(mirror, gitops.default_branch(mirror)), conn)
            repo_row = conn.get(Repo, repo_id)
            if repo_row is not None:
                repo_row.last_ingest_at = datetime.now(UTC)
                repo_row.last_ingested_sha = fetch.head_sha or repo_row.last_ingested_sha
                repo_row.last_ingested_refs = gitops.ref_tips(fetch.path)

        result.commits_added = stats.commits_written
        result.files_created = stats.files_created

        # History that has been rewritten away still counts toward N.
        removed = _drop_unreachable_commits(repo_id, fetch.path)
        if removed:
            log.info("repo %d: removed %d commit(s) no longer in git", repo_id, removed)

        with session_scope() as conn:
            repo_row = conn.get(Repo, repo_id)
            stale_aggregate = bool(
                repo_row is not None and repo_row.last_aggregate_sha != repo_row.last_ingested_sha
            )
        if stats.commits_written > 0 or removed or force_full or stale_aggregate:
            with session_scope() as conn:
                agg = rebuild_repo(repo_id, conn)
                result.pairs = agg.file_pairs
            with session_scope() as conn:
                score_repo(repo_id, conn)
            # Only now is the repository's derived state actually current.
            with session_scope() as conn:
                repo_row = conn.get(Repo, repo_id)
                if repo_row is not None:
                    repo_row.last_aggregate_sha = repo_row.last_ingested_sha

        with session_scope() as conn:
            repo_row = conn.get(Repo, repo_id)
            if repo_row is not None:
                repo_row.ingest_status = "ready"
                repo_row.ingest_duration_s = time.monotonic() - started

        result.status = "success"

    except Exception as exc:
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("repository %s failed", record.full_name)
        if result.repo_id is not None:
            try:
                with session_scope() as conn:
                    repo_row = conn.get(Repo, result.repo_id)
                    if repo_row is not None:
                        repo_row.ingest_status = "failed"
                        repo_row.ingest_error = result.error[:4000]
            except Exception:
                log.exception("could not record failure status for %s", record.full_name)

    result.duration_s = time.monotonic() - started
    return result


def run_ingest(
    records: list[RepoRecord] | None = None,
    trigger: str = "manual",
    force_full: bool = False,
    concurrency: int | None = None,
) -> RunResult:
    """Run the full pipeline over a set of repositories."""
    started = time.monotonic()

    with session_scope() as conn:
        if not _try_ingest_lock(conn):
            log.warning("another ingest run holds the lock; skipping this one")
            run = RunResult(kind="full" if force_full else "sync")
            run.status = "skipped"
            run.duration_s = time.monotonic() - started
            return run

        return _run_ingest_locked(
            records, trigger, force_full, concurrency, started
        )


def _aborted_run(
    exc: Exception, force_full: bool, trigger: str, started: float
) -> RunResult:
    """Record a run that refused to start, so the failure is visible in history."""
    log.error("aborting run: %s", exc)
    run = RunResult(kind="full" if force_full else "sync")
    run.run_id = _start_run(run.kind, trigger, 0)
    run.duration_s = time.monotonic() - started
    run.status = "failed"
    with session_scope() as conn:
        row = conn.get(models().IngestRun, run.run_id)
        if row is not None:
            row.status = "failed"
            row.finished_at = datetime.now(UTC)
            row.duration_s = run.duration_s
            row.error = str(exc)[:4000]
    return run


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

    drift = schema_drift()
    if drift:
        exc = AuthError(
            f"This service expects schema version {SCHEMA_VERSION} but the "
            f"database is at {SCHEMA_VERSION + drift}. It is running older code "
            "than the database was migrated to, which happens when only some "
            "services were rebuilt. Run `make up` to rebuild them all."
        )
        return _aborted_run(exc, force_full, trigger, started)

    try:
        verify_credentials(required=private_repos_in_scope() > 0)
        derived.ensure_current()
    except AuthError as exc:
        return _aborted_run(exc, force_full, trigger, started)

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

    if run.commits_added > 0 or force_full or _crossrepo_rebuild_needed():
        try:
            db = depbump.rebuild(force=force_full)
            log.info(
                "manifest bumps: %d repos scanned, %d new edges in %.1fs",
                db.repos_scanned, db.edges_written, db.duration_s,
            )
        except Exception:
            log.exception("manifest bump scan failed")

        try:
            with session_scope() as conn:
                depbump.refresh_declared(conn=conn, force=force_full)
                depbump.refresh_modules(conn=conn)
        except Exception:
            log.exception("declared dependency refresh failed")

        # Impact ranks the declared graph, so it runs after both stages above.
        try:
            pr = predict.rebuild(force=force_full)
            log.info(
                "impact: %d edges across %d repos in %.1fs",
                pr.rows_written, pr.sources, pr.duration_s,
            )
        except Exception:
            log.exception("impact prediction failed")

        try:
            mn = mining.rebuild(force=force_full)
            log.info(
                "mining: %d modules, %d drift rows, %d risk rows in %.1fs",
                mn.clusters, mn.drift_rows, mn.risk_rows, mn.duration_s,
            )
        except Exception:
            log.exception("mining rebuild failed")

    run.duration_s = time.monotonic() - started
    _prune_call_log()
    try:
        from git_synapse.analysis.query import duplicate_histories

        for dup in duplicate_histories():
            log.warning(
                "the same history is stored %d times on %s (%s commits): %s -- "
                "corpus-wide totals count it once per copy; pause all but one",
                dup["copies"], dup["host"], dup["commits"], ", ".join(dup["names"]))
    except Exception:
        log.debug("could not check for duplicated histories", exc_info=True)

    _finish_run(run)
    log.info(
        "ingest run %s finished in %.1fs: %d ok, %d failed, %d commits added",
        run.run_id, run.duration_s, len(run.ok), len(run.failed), run.commits_added,
    )
    return run


def load_repo_records() -> list[RepoRecord]:
    """Rebuild :class:`RepoRecord` objects from the database."""
    columns = (
        "github_id", "owner", "name", "full_name", "provider", "host",
        "clone_url", "default_branch", "disk_usage_kb", "is_private", "is_fork",
        "is_archived", "description", "homepage", "html_url", "ssh_url",
        "primary_language", "topics", "license_spdx", "visibility",
        "is_template", "is_disabled", "stargazers", "watchers", "forks_count",
        "open_issues", "github_created_at", "github_updated_at",
        "github_pushed_at",
    )
    Repo = models().Repo
    with session_scope() as conn:
        rows = conn.query(Repo).filter(Repo.is_enabled.is_(True)).order_by(Repo.id).all()

    booleans = {"is_private", "is_fork", "is_archived", "is_template", "is_disabled"}
    counts = {"stargazers", "watchers", "forks_count", "open_issues"}
    out = []
    for row in rows:
        fields = {column: getattr(row, column) for column in columns}
        for key in booleans:
            fields[key] = bool(fields[key])
        for key in counts:
            fields[key] = fields[key] or 0
        fields["topics"] = list(fields["topics"] or [])
        out.append(RepoRecord(**fields))
    return out
