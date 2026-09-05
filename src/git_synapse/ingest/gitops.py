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
            # Only a fallback: git needs a writable HOME for its config, and
            # the container sets one. Never used to hold anything.
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

    # Tags are fetched: a manifest that pins `v1.2.3` names a release, and
    # `ref_tag` is what turns that into a commit. Excluding them made every
    # ecosystem that pins by version rather than by SHA resolve to nothing,
    # silently -- the tag index was built and stayed empty.
    args = ["clone", "--bare", "--quiet"]
    if blobless:
        args.append("--filter=blob:none")
    args += [clone_url, str(staging)]

    log.info("cloning %s mirror -> %s", "blobless" if blobless else "full", path)
    try:
        run_git_network(args)
        # Mirror refspec so future fetches track every branch and every tag,
        # not just HEAD. Written explicitly because a bare clone has no fetch
        # refspec of its own: without this, `git fetch` updates FETCH_HEAD and
        # nothing else, and the mirror silently stops moving.
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
    args += [clone_url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
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
    """Every tip the walk visits: the default branch, and every tag.

    The watermark for an incremental walk, so it has to cover exactly what the
    walk covers. Excluding only the branch tip would re-read every release
    commit on each run; excluding a branch the walk never visits would skip
    commits that must still be read when that branch merges.
    """
    # `rev-list --no-walk` peels to commits; `rev-parse --tags` would hand back
    # the tag *object* for an annotated tag, which is not a commit and which the
    # watermark check below would discard on every run.
    proc = run_git(
        ["rev-list", "--no-walk", "--tags", "HEAD"],
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
    #: The commit on the shipping branch this release was cut from. Equal to
    #: ``commit_sha`` when the tag sits on that branch; the merge-base when it
    #: sits on a release branch; None when the histories are unrelated.
    main_sha: str | None = None


def read_tags(path: Path, default_branch: str | None = None) -> list[Tag]:
    """Every tag in a mirror, peeled, in one git call.

    An annotated tag points at a tag *object* which points at the commit, so
    `%(objectname)` is the wrong field for half of them; `%(*objectname)` is the
    peeled target and is empty for lightweight tags. Asking git to do the
    peeling avoids a `rev-parse` per tag, and its date is the tagger's for an
    annotated tag and the committer's otherwise -- which is the date a release
    was actually cut.
    """
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
        # `git tag` will name a blob or a tree as readily as a commit, and their
        # object ids are forty hex characters too -- so the shape check below
        # cannot tell them apart. Such a tag is not a release: it resolves to no
        # commit, and indexing it puts a non-commit in the version index.
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
    """Give every tag a commit on the shipping branch.

    Projects that cut a release branch tag *on that branch*, so the tagged
    commit is never walked and resolves to nothing: 116 of guava's 123 tags
    point off the branch that ships. The merge-base is the commit the release
    was cut from, which is on the branch and therefore already ingested.

    One `rev-list` establishes which commits are both on the branch and stored,
    so the slower path runs only for the tags that actually need it. It asks for
    `--no-merges` because a tag pointing straight at a merge commit -- which is
    how Prometheus tags nearly half its releases -- is on the branch yet names a
    row that was never written, and treating "on the branch" as "resolvable"
    left 247 of its tags anchored to nothing.
    """
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
        # No merge-base means unrelated histories -- an imported tree or an
        # orphan branch. Left None rather than anchored to something arbitrary.
        found = mb.stdout.strip() if mb.returncode == 0 else ""
        anchored.append(replace(tag, main_sha=_first_real_commit(path, found)))
    return anchored


def replayed_commits(path: Path, branch: str | None) -> set[str]:
    """Commits off the branch whose diff already exists on it.

    A fix landed on the shipping branch and then cherry-picked onto a release
    branch is the same change twice, and counting the second would say those
    files belong together on evidence that is really one observation repeated.

    `--cherry-mark` is git's own answer: it compares by patch id, normalised for
    whitespace and line offsets, so it recognises a backport that had to shift
    to apply -- and it does *not* recognise one that had to touch extra files,
    which is correct, because that is a different change.

    Only the divergent commits are compared, so the cost follows how much lives
    off the branch rather than the size of the history.
    """
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
    """`sha` itself, or the newest non-merge commit before it on the same line.

    The walk skips merges, because a merge restates its parents' changes. A
    merge-base often *is* a merge, and anchoring to one names a commit that was
    deliberately never stored -- 27 of auto's tags landed exactly there.
    Following first parents keeps to the branch's own line of development
    rather than wandering into a side branch that was merged in.
    """
    if not sha:
        return None
    proc = run_git(["rev-list", "--first-parent", "--no-merges", "-n", "1", sha],
                   cwd=path, check=False, timeout=60)
    return (proc.stdout.strip() or sha) if proc.returncode == 0 else sha


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
