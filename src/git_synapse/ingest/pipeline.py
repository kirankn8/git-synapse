"""End-to-end orchestration: discover -> mirror -> parse -> load -> aggregate -> score.

Repositories are independent of one another at every stage, so the pipeline
fans out across a thread pool. The work is almost entirely I/O -- git talking to
GitHub and PostgreSQL talking to the application -- so threads are the right primitive
despite the GIL; the CPU-bound part (vectorised scoring) releases the GIL inside
numpy anyway.

Failure is isolated per repository. One repo that has been deleted upstream, or
whose history is corrupt, records its error in ``ingest_run_repo`` and leaves
the other 269 to finish. A run reports ``partial`` rather than ``failed`` when
some repos succeeded, because "263 of 270 refreshed" is a materially different
operational situation from "nothing ran".
"""

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
from git_synapse.db.engine import SCHEMA_VERSION, connection, schema_drift
from git_synapse.db.orm import models
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

#: Consecutive network-failed repositories before a run gives up. Set above the
#: worker count so a single unlucky burst cannot trip it.
NETWORK_FAILURE_ABORT = 12

#: A discovery returning less than this fraction of the repositories already
#: known is treated as a failed listing rather than as the org having shrunk.
DISCOVERY_SHRINK_FLOOR = 0.8

#: Advisory lock key serialising ingest runs across processes. Arbitrary but
#: fixed; anything else taking this key would deadlock with the pipeline.
INGEST_LOCK_KEY = 0x0C047E5


def _try_ingest_lock(session: object) -> bool:
    """Acquire a transaction-scoped ORM row lock for the ingest run.

    The lock is represented by one well-known ``meta`` row.  This keeps the
    serialization primitive in the mapped schema and avoids database-specific
    advisory-lock SQL.  The surrounding session remains open for the whole
    run, so PostgreSQL releases the row lock if the process dies.
    """
    Meta = models().Meta
    try:
        row = session.get(Meta, "lock:ingest", with_for_update={"nowait": True})
        if row is None:
            session.add(Meta(key="lock:ingest", value={"owner": "ingest"}))
            session.flush()
        return True
    except (OperationalError, DBAPIError) as exc:
        if getattr(getattr(exc, "orig", None), "args", None):
            # PostgreSQL reports NOWAIT contention as SQLSTATE 55P03 through
            # different database-driver exception classes.
            detail = str(exc.orig.args[0])
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
    cutoff = datetime.now(UTC).timestamp() - max_age_hours * 3600
    cutoff_at = datetime.fromtimestamp(cutoff, tz=UTC)
    IngestRun = models().IngestRun
    with connection() as conn:
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
    with connection() as conn:
        row = conn.query(IngestRun).filter(IngestRun.status == "running").order_by(
            IngestRun.started_at.desc()
        ).first()
    if row is None:
        return None
    return {"id": int(row.id), "kind": row.kind, "trigger": row.trigger, "started_at": row.started_at}


def _start_run(kind: str, trigger: str, repos_total: int) -> int:
    IngestRun = models().IngestRun
    with connection() as conn:
        row = IngestRun(kind=kind, trigger=trigger, repos_total=repos_total)
        conn.add(row)
        conn.flush()
        return int(row.id)


def _prune_call_log() -> None:
    """Trim the call log at the end of a run.

    It is the one table that grows with traffic rather than with history, so it
    needs a bound something actually applies. A refresh is the natural place:
    it happens on a schedule, and a failure here must not fail the run.
    """
    from git_synapse.analysis import calls

    try:
        removed = calls.prune()
        if removed:
            log.info("call log: pruned %d row(s)", removed)
    except Exception:
        log.warning("could not prune the call log", exc_info=True)

    # Expired sessions are dead weight and, kept forever, a record of who was
    # signed in from where long after it could matter.
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
    """One sentence explaining a failed run, or None when nothing failed.

    A run whose repositories all fail identically is one systemic problem --
    a migration, a credential, a full disk -- not N independent ones, and the
    shared message is the whole diagnosis. Without this the row said `failed`
    with an empty error, and the per-repository table could not help either:
    it is keyed on a repository id, and a repository that fails before it has
    one records nothing at all. Which is exactly the earliest, most systemic
    failures.
    """
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
    with connection() as conn:
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
    with connection() as conn:
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
    with connection() as conn:
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
    """How many repositories about to be mirrored are private.

    A token is only genuinely required for those. An entirely public corpus --
    a public organisation, or an allowlist of public repositories -- clones over
    plain HTTPS and needs no credential at all.
    """
    Repo = models().Repo
    with connection() as conn:
        return int(conn.query(Repo).filter(Repo.is_enabled.is_(True), Repo.is_private.is_(True)).count())


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
            # No count: the listing failed, so nothing is known about how
            # many repositories this source has -- and they did not vanish.
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

    # A discovery that collapses is a symptom, not a fact about the accounts: an
    # unauthenticated request returns only public repositories, with HTTP 200 and
    # no error, and the run then quietly refreshes a fraction of the corpus.
    #
    # The comparison is against what the API *listed*, not what survived the
    # filters. Measuring the filtered count made narrowing an allowlist
    # indistinguishable from a broken credential, and refused the configuration
    # change with an error about the credential. Skipped when an account errored,
    # since then the shrinkage is explained and already reported.
    Repo = models().Repo
    with connection() as conn:
        known = int(conn.query(Repo).filter(Repo.is_enabled.is_(True)).count())
    if not failures and known and listed < known * DISCOVERY_SHRINK_FLOOR:
        raise AuthError(
            f"the API listed {listed} repositories but {known} are already known. "
            "That is the shape of an unauthenticated or partial listing, not "
            "repositories disappearing. Check the credential; nothing was changed."
        )

    with connection() as conn:
        for record in selected:
            upsert_repo(record, conn, account_id=owners.get(record.full_name))
    # After the writes, never before: what was selected is an intention, and a
    # run that aborts between the two leaves a source claiming repositories no
    # row backs.
    accounts.refresh_repo_counts()
    log.info("discovery upserted %d repositories from %d accounts", len(selected), len(configured))
    return selected


def _discover_account(account: dict) -> tuple[list[RepoRecord], int]:
    """List and filter one source, returning the kept records and the raw count.

    The raw count is what the shrink guard has to reason about: narrowing an
    allowlist legitimately collapses the *filtered* result, while a credential
    that has stopped working collapses the *listing*.

    For a source that names its repositories, the cheap way to get them depends
    entirely on how big the owner is, which is not knowable in advance -- and
    guessing it from the number of names is how a fixed threshold gets this
    exactly backwards. Naming seven repositories in an org that holds thirty
    costs seven requests by name and *one* by listing; naming one out of
    microsoft's 8,296 costs one by name and eighty-three by listing.

    So the first page is fetched either way -- one request, which the listing
    needs anyway -- and it reports how many the owner has. The choice is then
    arithmetic rather than a guess:

    * the owner fits in that page, so there is nothing left to fetch;
    * or the remaining pages cost less than the remaining names, so keep going;
    * or the names are cheaper, so ask for exactly those.
    """
    cfg = accounts.config_for(account)
    # Built through `parse` rather than by hand. Assembling a Source field by
    # field here meant `api_url` came straight from the account row, where NULL
    # means "the provider's public API" -- and `has_api` reads None as "no API
    # at all", so every ordinary GitHub source fell through to the no-API
    # fallback and re-imported 164 repositories as bare git URLs.
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

    # Impatient, unlike the clone path. Waiting out a rate limit is right for
    # one repository's mirror; across a hundred sources it is not -- five
    # retries of sixty seconds each, per source, is most of a day asleep inside
    # a single run. Discovery repeats hourly, so giving up on a source and
    # recording why costs nothing that the next run does not recover.
    with providers.for_source(source, token=token, patient=False) as client:
        if not client.supports_listing():
            if not only:
                log.warning("%s has no API to enumerate; add its repositories by URL",
                            login)
                return [], 0
            # Nothing to compare against: by name is the only way in. Note that
            # a host with no API cannot tell us a name is wrong, so a stale
            # entry becomes a record that fails at clone time instead.
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
                # The owner is large and the allowlist is short: ask for exactly
                # what was named and stop paging.
                return _fetch_by_name(client, login, only), len(only)
            records = list(first.records)
            page = 1
            while True:
                page += 1
                nxt = client.list_page(login, page)
                records.extend(nxt.records)
                if not nxt.has_more:
                    break

    if only:
        # An allowlist is an explicit answer, so it decides on its own; running
        # the include filters over it as well would let `include_forks` drop a
        # repository somebody named.
        #
        # Matched on the path *under the owner*, never on the bare last segment.
        # On GitHub the two are the same. On GitLab they are not: `veloren`
        # also matches `veloren/dev/veloren`, a different project that happens
        # to share a name -- and in that case a byte-identical history, which
        # was then counted twice in every corpus-wide total.
        wanted = {n.lower().strip("/") for n in only}
        prefix = f"{login.lower()}/"
        kept = [r for r in records
                if r.full_name.lower() in wanted
                or r.full_name.lower().removeprefix(prefix) in wanted]
        return kept, len(records)
    return select_repos(records, cfg), len(records)


def _fetch_by_name(client, login: str, names: list[str]) -> list[RepoRecord]:
    """Fetch exactly the repositories an allowlist names, one request each.

    One bad name must not cost the others: a repository that was renamed or
    deleted upstream is a fact about that repository, not about the source.
    """
    out: list[RepoRecord] = []
    for name in names:
        try:
            out.append(client.get_repo(login, name))
        except Exception as exc:  # noqa: BLE001 - recorded, then carry on
            log.warning("could not fetch %s/%s: %s", login, name, exc)
    return out


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

    Reachability must be measured over exactly the refs the ingest walks, which
    is the shipping branch *and* every release tag. Measuring from the branch
    alone made this prune delete every commit the tag walk had just inserted --
    and delete it silently, because the run counts what the loader wrote rather
    than what survived. It is self-triggering, too: the new commits push the
    stored count above the branch count, which is the very condition that runs
    the prune.
    """
    Commit = models().Commit
    with connection() as conn:
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

    with connection() as conn:
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
        with connection() as conn:
            repo_id = upsert_repo(record, conn)
        result.repo_id = repo_id

        blobless = gitops.choose_clone_mode(record.disk_usage_kb, cfg.ingest)
        result.blobless = blobless

        # This repository's own source credential wins over the deployment
        # one; `authed_clone_url` then refuses to embed either on a host that
        # did not issue it.
        token = _clone_token(record) or cfg.github.current_token()
        fetch = gitops.sync_mirror(
            record.full_name,
            record.authed_clone_url(token),
            public_url=record.clone_url,
            blobless=blobless,
            host=record.host,
        )
        result.cloned = fetch.cloned

        Repo = models().Repo
        with connection() as conn:
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
            include_tags=True,
        )

        with connection() as conn:
            stats = load_commits(repo_id, commits, conn)
            # Marked after loading, because a replay is only recognisable by
            # comparing against the branch, and the flag is what keeps the same
            # change from being counted once per release branch it reached.
            branch = gitops.default_branch(fetch.path)
            _mark_replays(repo_id, gitops.replayed_commits(fetch.path, branch), conn)
            # After the commits, so each tag resolves to a row rather than
            # leaving commit_id null on the first run.
            mirror = gitops.mirror_path_for(record.full_name, host=record.host)
            load_tags(repo_id, gitops.read_tags(mirror, gitops.default_branch(mirror)), conn)
            # Record the default branch tip as the next run's exclusion point.
            # This was every branch tip when the walk covered every branch;
            # excluding more than the walk visits would skip commits that must
            # still be read when their branch merges.
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

        # Re-derive aggregates when the atomic data moved, or when a previous
        # pass ingested commits but failed before aggregating them. Without the
        # second condition that failure was permanent: the commit watermark had
        # already advanced, so every later run saw nothing to do.
        with connection() as conn:
            repo_row = conn.get(Repo, repo_id)
            stale_aggregate = bool(
                repo_row is not None and repo_row.last_aggregate_sha != repo_row.last_ingested_sha
            )
        if stats.commits_written > 0 or removed or force_full or stale_aggregate:
            with connection() as conn:
                agg = rebuild_repo(repo_id, conn)
                result.pairs = agg.file_pairs
            with connection() as conn:
                score_repo(repo_id, conn)
            # Only now is the repository's derived state actually current.
            with connection() as conn:
                repo_row = conn.get(Repo, repo_id)
                if repo_row is not None:
                    repo_row.last_aggregate_sha = repo_row.last_ingested_sha

        with connection() as conn:
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
                with connection() as conn:
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
    """Run the full pipeline over a set of repositories.

    Args:
        records: repositories to process. Discovered from GitHub when omitted.
        trigger: ``manual``, ``schedule`` or ``api``; recorded on the run.
        force_full: ignore watermarks and re-read every history.
        concurrency: worker threads; defaults to ``INGEST_CONCURRENCY``.

    Returns:
        A :class:`RunResult` summarising every repository.
    """
    started = time.monotonic()

    # One ingest at a time, across processes. The mapped meta row is locked for
    # this transaction and released automatically if the process exits.
    with connection() as conn:
        if not _try_ingest_lock(conn):
            log.warning("another ingest run holds the lock; skipping this one")
            run = RunResult(kind="full" if force_full else "sync")
            run.status = "skipped"
            run.duration_s = time.monotonic() - started
            return run

        return _run_ingest_locked(
            records, trigger, force_full, concurrency, started
        )


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
    # Older code than the database was migrated to. Every write would fail
    # against a constraint this process still expects, one repository at a
    # time, so refuse once with something a reader can act on.
    drift = schema_drift()
    if drift:
        exc = AuthError(
            f"This service expects schema version {SCHEMA_VERSION} but the "
            f"database is at {SCHEMA_VERSION + drift}. It is running older code "
            "than the database was migrated to, which happens when only some "
            "services were rebuilt. Run `make up` to rebuild them all."
        )
        log.error("aborting run: %s", exc)
        run = RunResult(kind="full" if force_full else "sync")
        run.run_id = _start_run(run.kind, trigger, 0)
        run.duration_s = time.monotonic() - started
        run.status = "failed"
        with connection() as conn:
            row = conn.get(models().IngestRun, run.run_id)
            if row is not None:
                row.status = "failed"
                row.finished_at = datetime.now(UTC)
                row.duration_s = run.duration_s
                row.error = str(exc)[:4000]
        return run

    try:
        verify_credentials(required=private_repos_in_scope() > 0)
        # Materialised analytics are versioned separately from source data.
        # This catches calculation changes even when no repository has new
        # commits, and runs the affected dependency closure exactly once.
        derived.ensure_current()
    except AuthError as exc:
        log.error("aborting run: %s", exc)
        run = RunResult(kind="full" if force_full else "sync")
        run.run_id = _start_run(run.kind, trigger, 0)
        run.duration_s = time.monotonic() - started
        run.status = "failed"
        with connection() as conn:
            row = conn.get(models().IngestRun, run.run_id)
            if row is not None:
                row.status = "failed"
                row.finished_at = datetime.now(UTC)
                row.duration_s = run.duration_s
                row.error = str(exc)[:4000]
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
    if cfg.crossrepo.enabled and (
        run.commits_added > 0 or force_full or _crossrepo_rebuild_needed()
    ):
        # Manifest bumps first: they are incremental per repository, and they
        # are what dates every edge the graph below carries.
        try:
            db = depbump.rebuild(force=force_full)
            log.info(
                "manifest bumps: %d repos scanned, %d new edges in %.1fs",
                db.repos_scanned, db.edges_written, db.duration_s,
            )
        except Exception:
            log.exception("manifest bump scan failed")

        try:
            # Keep both structural graphs in one transaction. If the module
            # refresh fails, the declared graph remains stale too and the next
            # ordinary refresh retries both stages.
            with connection() as conn:
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
    # Loud, and at the end, where a reader is already looking at the run: a
    # duplicated history makes every corpus-wide total wrong, and nothing about
    # either row on its own looks it.
    try:
        from git_synapse.analysis.query import duplicate_histories

        for dup in duplicate_histories():
            log.warning(
                "the same history is stored %d times on %s (%s commits): %s -- "
                "corpus-wide totals count it once per copy; pause all but one",
                dup["copies"], dup["host"], dup["commits"], ", ".join(dup["names"]))
    except Exception:
        # The work is already done and recorded. A report that cannot run is
        # not a reason to lose it.
        log.debug("could not check for duplicated histories", exc_info=True)

    _finish_run(run)
    log.info(
        "ingest run %s finished in %.1fs: %d ok, %d failed, %d commits added",
        run.run_id, run.duration_s, len(run.ok), len(run.failed), run.commits_added,
    )
    return run


def load_repo_records() -> list[RepoRecord]:
    """Rebuild :class:`RepoRecord` objects from the database.

    Lets a refresh run without calling any host's API, which is useful when
    re-processing after a config change or when the API is rate limited.

    Every descriptive column is read back, not just the handful the pipeline
    needs. The record is written straight back out by ``upsert_repo``, so a
    partial read here silently blanked everything it omitted: language,
    description, topics, licence and stars were wiped on every ingest.

    Columns are named once and zipped into keyword arguments rather than read
    by position. A positional read is how a column added in the middle of a
    A positional projection shifts every field after it -- silently, wherever the types
    happen to be compatible.
    """
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
    with connection() as conn:
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
