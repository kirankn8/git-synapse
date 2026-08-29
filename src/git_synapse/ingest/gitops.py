"""Git mirror management: bare clones in full or blobless mode, plus fetches.

Two mirror modes
----------------
**full** -- an ordinary bare clone. Carries blob objects, so ``git log`` can
compute ``--numstat`` line counts and perform *inexact* rename detection
(``-M50%``). This is the default because it yields strictly richer atomic data.

**blobless** -- ``--filter=blob:none``. One to two orders of magnitude smaller,
but blob contents are absent, which has two hard consequences verified against
real repositories:

* ``--numstat`` **cannot** be used. Line counts require reading file contents,
  and asking for them makes git try to lazily fetch every blob from the
  promisor remote -- which either fails outright or hangs on the network.
* Rename detection must be pinned to ``-M100%``. Exact renames are resolvable
  by comparing blob SHAs in the tree, but inexact ones need content.

``--raw -M100%`` walks a blobless mirror completely and correctly, which is all
the 29 association measures require: they depend only on *which paths* co-occur
in a commit, never on how many lines changed. A blobless repo therefore yields
complete coupling statistics and merely loses churn as an extra attribute.

The mode is chosen per repository by size, so a single 9.8 GB documentation
monorepo does not force the whole org onto the degraded path.

Every git invocation sets ``GIT_NO_LAZY_FETCH=1``. On a blobless mirror that
converts a potential indefinite network hang into an immediate, visible error.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from git_synapse.config import IngestConfig, get_config

log = logging.getLogger(__name__)


class GitError(RuntimeError):
    """A git subprocess exited non-zero."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args[:4])}... exited {returncode}: {self.stderr[:500]}")


@dataclass
class FetchResult:
    """Outcome of syncing one mirror."""

    path: Path
    head_sha: str | None
    cloned: bool
    changed: bool
    duration_s: float
    blobless: bool = False
    size_kb: int = 0


def _base_env() -> dict[str, str]:
    """Environment for every git call.

    Disables interactive prompting so a bad credential fails fast instead of
    hanging a worker, and skips the user's global config so behaviour is
    identical inside and outside the container.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "echo",
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_NOSYSTEM": "1",
            # On a blobless mirror an accidental blob read would otherwise
            # block on the promisor remote. Fail loudly instead of hanging.
            "GIT_NO_LAZY_FETCH": "1",
            "HOME": env.get("HOME", "/tmp"),
            "LC_ALL": "C",
        }
    )
    return env


def run_git(
    args: list[str],
    cwd: Path | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the completed process.

    Args:
        args: arguments after the ``git`` executable.
        cwd: working directory, normally the mirror path.
        timeout: seconds before the process is killed; falls back to config.
        check: raise :class:`GitError` on a non-zero exit.

    Raises:
        GitError: when ``check`` is set and git fails.
    """
    cfg = get_config().ingest
    timeout = timeout or cfg.git_timeout
    cmd = ["git", *args]
    proc = subprocess.run(  # noqa: S603 - fixed executable, args built internally
        cmd,
        cwd=str(cwd) if cwd else None,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc


#: Substrings that identify a transient network failure rather than a real
#: problem with the repository. Matched case-insensitively against git's stderr.
#: A DNS blip or a reset connection must not fail a repository permanently --
#: over a 270-repo run, an occasional one is close to certain.
TRANSIENT_ERROR_MARKERS = (
    "could not resolve host",
    "connection reset",
    "connection timed out",
    "connection refused",
    "temporary failure in name resolution",
    "unexpected disconnect",
    "early eof",
    "rpc failed",
    "the remote end hung up",
    "unable to access",
    "gnutls_handshake",
    "ssl_read",
    "operation timed out",
    "failed to connect",
    "http/2 stream",
    "error in the http2 framing",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
)

#: Substrings identifying a PERMANENT failure: retrying or re-cloning cannot
#: help, because the problem is credentials or the repository itself.
#:
#: Distinguishing these matters more than it looks. An expired token made every
#: fetch fail, `sync_mirror` treated that like a corrupt mirror and fell back to
#: a fresh clone, and the clone deleted the existing mirror before failing on the
#: same auth error -- destroying 213 of 272 working mirrors in one run.
PERMANENT_ERROR_MARKERS = (
    "authentication failed",
    "invalid username or token",
    "password authentication is not supported",
    "could not read username",
    "permission denied",
    "repository not found",
    "does not exist",
    "access denied",
    "403 forbidden",
    "401 unauthorized",
)

#: Attempts for a network-touching git operation.
NETWORK_RETRIES = 4


def is_permanent_error(stderr: str) -> bool:
    """True if retrying or re-cloning cannot possibly help."""
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in PERMANENT_ERROR_MARKERS)


def is_transient_error(stderr: str) -> bool:
    """True if git's stderr looks like a temporary network problem."""
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in TRANSIENT_ERROR_MARKERS)


def run_git_network(
    args: list[str],
    cwd: Path | None = None,
    timeout: int | None = None,
    attempts: int = NETWORK_RETRIES,
) -> subprocess.CompletedProcess[str]:
    """Run a git command that talks to a remote, retrying transient failures.

    Permanent failures -- a deleted repository, a bad credential, a missing ref --
    are raised on the first attempt rather than retried, so a genuinely broken
    repo still fails fast.

    Raises:
        GitError: on a permanent failure, or after the last attempt.
    """
    last: GitError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return run_git(args, cwd=cwd, timeout=timeout, check=True)
        except GitError as exc:
            if not is_transient_error(exc.stderr):
                raise
            last = exc
            if attempt == attempts:
                break
            backoff = min(2**attempt, 20)
            log.warning(
                "transient git failure (attempt %d/%d), retrying in %ds: %s",
                attempt, attempts, backoff, exc.stderr[:160],
            )
            time.sleep(backoff)
        except subprocess.TimeoutExpired as exc:
            last = GitError(args, -1, f"timed out after {exc.timeout}s")
            if attempt == attempts:
                break
            log.warning("git timed out (attempt %d/%d); retrying", attempt, attempts)

    assert last is not None
    raise last


def mirror_path_for(full_name: str, cfg: IngestConfig | None = None) -> Path:
    """Filesystem location of a repo's bare mirror.

    Uses ``<root>/<owner>/<name>.git``, mirroring GitHub's own layout so the
    directory tree stays browsable.
    """
    cfg = cfg or get_config().ingest
    owner, _, name = full_name.partition("/")
    return cfg.mirror_root / owner / f"{name}.git"


def is_valid_mirror(path: Path) -> bool:
    """True if ``path`` looks like a usable bare repository.

    Guards against a half-written clone left behind by a killed container: such
    a directory exists but has no HEAD, and reusing it would fail every
    subsequent fetch.
    """
    if not path.is_dir():
        return False
    try:
        proc = run_git(["rev-parse", "--is-bare-repository"], cwd=path, check=False, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def clone_mirror(
    clone_url: str,
    path: Path,
    public_url: str | None = None,
    blobless: bool = False,
) -> None:
    """Create a bare mirror at ``path``.

    Clones into a sibling temporary directory and swaps it into place only once
    the clone has succeeded. The obvious implementation -- remove the old mirror,
    then clone -- loses the existing mirror whenever the clone fails, which is
    exactly what happened when a token expired: 213 working mirrors were deleted
    and not replaced. A mirror is expensive to rebuild, so it must never be
    destroyed on the strength of an operation that has not completed.

    Args:
        clone_url: URL used for the clone; may embed a token.
        path: final destination directory.
        public_url: token-free URL to store as the remote afterwards, so the
            credential is never written into the mirror's config on disk.
        blobless: omit blob objects. See the module docstring for what this costs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".incoming")
    if staging.exists():
        shutil.rmtree(staging)

    args = ["clone", "--bare", "--no-tags", "--quiet"]
    if blobless:
        args.append("--filter=blob:none")
    args += [clone_url, str(staging)]

    log.info("cloning %s mirror -> %s", "blobless" if blobless else "full", path)
    try:
        run_git_network(args)
        # Mirror refspec so future fetches track every branch, not just HEAD.
        run_git(["config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"], cwd=staging)
        if public_url:
            run_git(["config", "remote.origin.url", public_url], cwd=staging)
    except BaseException:
        # Leave the previous mirror untouched on any failure.
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    # Swap: move the old aside, promote the new, then discard the old.
    retired = path.with_name(path.name + ".retired")
    if retired.exists():
        shutil.rmtree(retired, ignore_errors=True)
    if path.exists():
        path.rename(retired)
    staging.rename(path)
    if retired.exists():
        shutil.rmtree(retired, ignore_errors=True)


def fetch_mirror(
    path: Path,
    clone_url: str,
    public_url: str | None = None,
    blobless: bool = False,
) -> bool:
    """Fetch new refs into an existing mirror.

    The token-bearing URL is passed in argv for the duration of the fetch only,
    then the remote is reset to the public URL, so the credential is never
    persisted.

    Returns:
        True if any ref moved.
    """
    before = current_head(path)
    args = ["fetch", "--prune", "--quiet"]
    if blobless:
        args.append("--filter=blob:none")
    args += [clone_url, "+refs/heads/*:refs/heads/*"]
    run_git_network(args, cwd=path)
    if public_url:
        run_git(["config", "remote.origin.url", public_url], cwd=path, check=False)
    after = current_head(path)
    return before != after


def choose_clone_mode(github_size_kb: int | None, cfg: IngestConfig | None = None) -> bool:
    """Decide whether a repo should be mirrored blobless.

    Args:
        github_size_kb: repository size as reported by the GitHub API.
        cfg: ingest configuration.

    Returns:
        True to clone blobless.
    """
    cfg = cfg or get_config().ingest
    if cfg.force_blobless:
        return True
    if not github_size_kb:
        return False
    return github_size_kb > cfg.blobless_threshold_kb


def sync_mirror(
    full_name: str,
    clone_url: str,
    public_url: str | None = None,
    blobless: bool = False,
) -> FetchResult:
    """Ensure a current mirror exists for ``full_name``.

    Clones when absent or corrupt, fetches otherwise. A fetch failure falls back
    to a fresh clone only when the cause suggests the mirror itself is damaged.
    Auth failures and network failures both keep the existing mirror: neither
    says anything is wrong with it, and re-cloning on those destroyed 213 working
    mirrors once and wasted an hour of an outage the other time.
    """
    started = time.monotonic()
    path = mirror_path_for(full_name)
    cloned = False

    if not is_valid_mirror(path):
        clone_mirror(clone_url, path, public_url, blobless=blobless)
        cloned = True
        changed = True
    elif mirror_is_blobless(path) != blobless:
        # The configured mode changed since the last run (e.g. the size
        # threshold moved). Re-clone rather than serve mismatched data.
        log.info("clone mode changed for %s; re-cloning", full_name)
        clone_mirror(clone_url, path, public_url, blobless=blobless)
        cloned = True
        changed = True
    else:
        try:
            changed = fetch_mirror(path, clone_url, public_url, blobless=blobless)
        except GitError as exc:
            if is_permanent_error(exc.stderr):
                # Bad credentials or a repository that no longer exists. A
                # re-clone would fail identically, so surface the real error and
                # keep the mirror we already have.
                log.error(
                    "fetch failed for %s and the cause is not recoverable "
                    "(mirror preserved): %s",
                    full_name, exc.stderr[:200],
                )
                raise
            if is_transient_error(exc.stderr):
                # A network failure says nothing about the mirror, which is
                # still perfectly good. Re-cloning here threw away a working
                # mirror and spent ten minutes per repository failing to
                # replace it; during one outage that was 213 repositories. Fail
                # this repository and let the next run retry the fetch.
                log.warning(
                    "fetch failed for %s and the cause looks transient "
                    "(mirror preserved, will retry next run): %s",
                    full_name, exc.stderr[:200],
                )
                raise
            log.warning(
                "fetch failed for %s in a way that suggests a damaged mirror; "
                "re-cloning: %s", full_name, exc.stderr[:200],
            )
            clone_mirror(clone_url, path, public_url, blobless=blobless)
            cloned = True
            changed = True

    return FetchResult(
        path=path,
        head_sha=current_head(path),
        cloned=cloned,
        changed=changed,
        duration_s=time.monotonic() - started,
        blobless=blobless,
        size_kb=repo_size_kb(path),
    )


def mirror_is_blobless(path: Path) -> bool:
    """True if the mirror on disk was cloned with a blob filter.

    Read back from the repo's own config rather than trusted from the caller,
    so a mode change is detected even across restarts.
    """
    proc = run_git(
        ["config", "--get", "remote.origin.partialclonefilter"],
        cwd=path,
        check=False,
        timeout=30,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def current_head(path: Path) -> str | None:
    """SHA of the mirror's default branch tip, or None for an empty repo."""
    proc = run_git(["rev-parse", "HEAD"], cwd=path, check=False, timeout=60)
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def default_branch(path: Path) -> str | None:
    """Branch that HEAD points at inside the mirror."""
    proc = run_git(["symbolic-ref", "--short", "HEAD"], cwd=path, check=False, timeout=30)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def ref_tips(path: Path) -> list[str]:
    """SHA of the default branch tip, as a single-element list.

    The watermark for an incremental walk. It used to be every branch tip,
    because the walk visited every branch; the walk now follows only the branch
    that ships, so anything else would exclude commits that must still be read
    when they eventually merge.
    """
    proc = run_git(
        ["rev-parse", "HEAD"],
        cwd=path,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        return []
    # `git rev-parse HEAD` exits 0 and echoes the literal "HEAD" when the ref
    # does not resolve, which an empty repository always hits. Storing that as
    # the watermark would make the next run exclude `^HEAD` and read nothing --
    # harmless while the repository stays empty, and permanent once it does not.
    return [
        tip for tip in (line.strip() for line in proc.stdout.splitlines())
        if len(tip) == 40 and all(c in "0123456789abcdef" for c in tip)
    ]


@dataclass(frozen=True)
class Tag:
    """One release tag, already peeled to the commit it names."""

    name: str
    commit_sha: str
    tagged_at: datetime | None
    annotated: bool


def read_tags(path: Path) -> list[Tag]:
    """Every tag in a mirror, peeled, in one git call.

    An annotated tag points at a tag *object* which points at the commit, so
    `%(objectname)` is the wrong field for half of them; `%(*objectname)` is the
    peeled target and is empty for lightweight tags. Asking git to do the
    peeling avoids a `rev-parse` per tag, and its date is the tagger's for an
    annotated tag and the committer's otherwise -- which is the date a release
    was actually cut.
    """
    proc = run_git(
        ["for-each-ref", "--format=%(refname:short)\t%(objecttype)\t%(objectname)"
         "\t%(*objectname)\t%(creatordate:iso-strict)", "refs/tags"],
        cwd=path, check=False, timeout=300,
    )
    if proc.returncode != 0:
        return []

    tags: list[Tag] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        name, kind, obj, peeled, when = parts
        sha = peeled or obj
        if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
            continue
        try:
            tagged_at = datetime.fromisoformat(when) if when else None
        except ValueError:
            tagged_at = None
        tags.append(Tag(name=name, commit_sha=sha, tagged_at=tagged_at,
                        annotated=(kind == "tag")))
    return tags


def commit_exists(path: Path, sha: str) -> bool:
    """True if ``sha`` is present in the mirror.

    Used to validate the watermark from the previous run before asking git for
    a ``sha..HEAD`` range: a force-push can orphan the old tip, and passing a
    missing SHA to ``git log`` is a hard error.
    """
    if not sha:
        return False
    proc = run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=path, check=False, timeout=30)
    return proc.returncode == 0


def repo_size_kb(path: Path) -> int:
    """On-disk size of the mirror in KB, for the storage panel in the UI."""
    proc = run_git(["count-objects", "-v"], cwd=path, check=False, timeout=60)
    if proc.returncode != 0:
        return 0
    total = 0
    for line in proc.stdout.splitlines():
        if line.startswith(("size:", "size-pack:")):
            _, _, value = line.partition(":")
            total += int(value.strip() or 0)
    return total


def remove_mirror(full_name: str) -> bool:
    """Delete a mirror from disk. Returns True if something was removed."""
    path = mirror_path_for(full_name)
    if path.exists():
        shutil.rmtree(path)
        return True
    return False
