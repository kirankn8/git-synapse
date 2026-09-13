"""Git mirror management: bare clones in full or blobless mode, plus fetches."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
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
    """Environment for every git call."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "echo",
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "HOME": env.get("HOME", "/tmp"),  # noqa: S108
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
    """Run a git command and return the completed process."""
    cfg = get_config().ingest
    timeout = timeout or cfg.git_timeout
    cmd = ["git", *args]
    proc = subprocess.run(
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
    """Run a git command that talks to a remote, retrying transient failures."""
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


def mirror_path_for(full_name: str, cfg: IngestConfig | None = None,
                    host: str = "github.com") -> Path:
    """Filesystem location of a repo's bare mirror."""
    cfg = cfg or get_config().ingest
    owner, _, name = full_name.partition("/")
    root = cfg.mirror_root if host in ("", "github.com") else cfg.mirror_root / host
    return root / owner / f"{name}.git"


def is_valid_mirror(path: Path) -> bool:
    """True if ``path`` looks like a usable bare repository."""
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
    """Create a bare mirror at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".incoming")
    if staging.exists():
        shutil.rmtree(staging)

    args = ["clone", "--bare", "--quiet"]
    if blobless:
        args.append("--filter=blob:none")
    args += [clone_url, str(staging)]

    log.info("cloning %s mirror -> %s", "blobless" if blobless else "full", path)
    try:
        run_git_network(args)
        run_git(["config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"], cwd=staging)
        run_git(["config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*"],
                cwd=staging)
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
    """Fetch new refs into an existing mirror."""
    before = current_head(path)
    args = ["fetch", "--prune", "--quiet"]
    if blobless:
        args.append("--filter=blob:none")
    args += [clone_url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
    run_git_network(args, cwd=path)
    if public_url:
        run_git(["config", "remote.origin.url", public_url], cwd=path, check=False)
    after = current_head(path)
    return before != after


def choose_clone_mode(github_size_kb: int | None, cfg: IngestConfig | None = None) -> bool:
    """Decide whether a repo should be mirrored blobless."""
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
    host: str = "github.com",
) -> FetchResult:
    """Ensure a current mirror exists for ``full_name``."""
    started = time.monotonic()
    path = mirror_path_for(full_name, host=host)
    cloned = False

    if not is_valid_mirror(path):
        clone_mirror(clone_url, path, public_url, blobless=blobless)
        cloned = True
        changed = True
    elif mirror_is_blobless(path) != blobless:
        log.info("clone mode changed for %s; re-cloning", full_name)
        clone_mirror(clone_url, path, public_url, blobless=blobless)
        cloned = True
        changed = True
    else:
        try:
            changed = fetch_mirror(path, clone_url, public_url, blobless=blobless)
        except GitError as exc:
            if is_permanent_error(exc.stderr):
                log.error(
                    "fetch failed for %s and the cause is not recoverable "
                    "(mirror preserved): %s",
                    full_name, exc.stderr[:200],
                )
                raise
            if is_transient_error(exc.stderr):
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
    """True if the mirror on disk was cloned with a blob filter."""
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
    """Every tip the walk visits: the default branch, and every tag."""
    proc = run_git(
        ["rev-list", "--no-walk", "--tags", "HEAD"],
        cwd=path,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        return []
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
    main_sha: str | None = None


def read_tags(path: Path, default_branch: str | None = None) -> list[Tag]:
    """Every tag in a mirror, peeled, in one git call."""
    if not path.is_dir():
        return []
    proc = run_git(
        ["for-each-ref",
         ("--format=%(refname:short)\t%(objecttype)\t%(objectname)"
          "\t%(*objectname)\t%(*objecttype)\t%(creatordate:iso-strict)"),
         "refs/tags"],
        cwd=path, check=False, timeout=300,
    )
    if proc.returncode != 0:
        return []

    tags: list[Tag] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        name, kind, obj, peeled, peeled_kind, when = parts
        if (peeled_kind or kind) != "commit":
            continue
        sha = peeled or obj
        if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
            continue
        try:
            tagged_at = datetime.fromisoformat(when) if when else None
        except ValueError:
            tagged_at = None
        tags.append(Tag(name=name, commit_sha=sha, tagged_at=tagged_at,
                        annotated=(kind == "tag")))
    return _anchor_to_branch(path, tags, default_branch)


def _anchor_to_branch(path: Path, tags: list[Tag], branch: str | None) -> list[Tag]:
    """Give every tag a commit on the shipping branch."""
    if not branch or not tags:
        return tags
    proc = run_git(["rev-list", "--no-merges", branch], cwd=path, check=False, timeout=300)
    if proc.returncode != 0:
        return tags
    on_branch = set(proc.stdout.split())

    anchored: list[Tag] = []
    for tag in tags:
        if tag.commit_sha in on_branch:
            anchored.append(replace(tag, main_sha=tag.commit_sha))
            continue
        mb = run_git(["merge-base", branch, tag.commit_sha],
                     cwd=path, check=False, timeout=60)
        found = mb.stdout.strip() if mb.returncode == 0 else ""
        anchored.append(replace(tag, main_sha=_first_real_commit(path, found)))
    return anchored


def replayed_commits(path: Path, branch: str | None) -> set[str]:
    """Commits off the branch whose diff already exists on it."""
    if not branch or not path.is_dir():
        return set()
    proc = run_git(["for-each-ref", "--format=%(objectname)", "refs/tags"],
                   cwd=path, check=False, timeout=120)
    if proc.returncode != 0:
        return set()

    replays: set[str] = set()
    for tip in dict.fromkeys(proc.stdout.split()):
        # A tag already on the branch has nothing on the other side to compare.
        marked = run_git(["rev-list", "--cherry-mark", "--right-only", "--no-merges",
                          f"{branch}...{tip}"], cwd=path, check=False, timeout=120)
        if marked.returncode != 0:
            continue
        replays.update(line[1:] for line in marked.stdout.splitlines()
                       if line.startswith("="))
    return replays


def _first_real_commit(path: Path, sha: str) -> str | None:
    """`sha` itself, or the newest non-merge commit before it on the same line."""
    if not sha:
        return None
    proc = run_git(["rev-list", "--first-parent", "--no-merges", "-n", "1", sha],
                   cwd=path, check=False, timeout=60)
    return (proc.stdout.strip() or sha) if proc.returncode == 0 else sha


def commit_exists(path: Path, sha: str) -> bool:
    """True if ``sha`` is present in the mirror."""
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


