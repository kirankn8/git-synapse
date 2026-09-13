"""Streaming parser for ``git log`` output.

Output format
-------------
The pipeline invokes git with ``-z --raw --numstat`` and a custom ``--format``.
That produces a single NUL-separated record stream, empirically verified against
real repositories, laid out per commit as:

1. One header record, prefixed with ``\\x01`` and holding ten ``\\x1f``-separated
   fields (sha, parents, author, ..., subject, body).
2. A *raw* block: for each changed path, one record ``:<mode> <mode> <sha>
   <sha> <STATUS>`` followed by one path record -- or, for renames and copies,
   two path records (old then new).
3. A *numstat* block: one record per path, ``<adds>\\t<dels>\\t<path>``, except
   for renames and copies where the path is empty and the old and new paths
   follow as two further records. Binary files report ``-`` for both counts.

Both blocks are parsed because neither alone is sufficient: the raw block
carries the status letter and rename similarity, the numstat block carries line
counts. They are joined on the (new) path.

Everything is streamed. A repository with a million commits is processed with
one commit resident in memory at a time.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from git_synapse.config import get_config
from git_synapse.ingest.gitops import GitError, _base_env

log = logging.getLogger(__name__)

#: Marks the start of a commit header record.
COMMIT_SENTINEL = "\x01"
#: Separates fields inside the header record.
FIELD_SEP = "\x1f"

#: The --format string matching the ten header fields parsed below.
LOG_FORMAT = (
    "%x01%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%s%x1f%b"
)

#: Read size for the subprocess pipe.
_CHUNK = 1 << 20


@dataclass(slots=True)
class FileChange:
    """One file touched by one commit -- the atomic fact of this system."""

    path: str
    change_type: str = "M"
    insertions: int = 0
    deletions: int = 0
    is_binary: bool = False
    old_path: str | None = None
    similarity: int | None = None


@dataclass(slots=True)
class ParsedCommit:
    """A commit and every file it touched."""

    sha: str
    parents: list[str]
    author_name: str
    author_email: str
    authored_at: datetime
    committer_name: str
    committer_email: str
    committed_at: datetime
    subject: str
    body: str
    files: list[FileChange] = field(default_factory=list)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def insertions(self) -> int:
        return sum(f.insertions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)


def _parse_git_date(value: str) -> datetime:
    """Parse git's strict-ISO ``%aI`` output.

    Falls back to the Unix epoch rather than raising: a handful of commits in
    any large org carry corrupt author dates, and losing the whole repository
    over one of them would be the wrong trade.
    """
    try:
        return datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        log.debug("unparseable git date %r; defaulting to epoch", value)
        return datetime.fromtimestamp(0).astimezone()


def _iter_records(stream) -> Iterator[str]:
    """Yield NUL-separated records from a binary stream without buffering it all."""
    buffer = b""
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        buffer += chunk
        *complete, buffer = buffer.split(b"\x00")
        for record in complete:
            yield record.decode("utf-8", errors="replace")
    if buffer:
        yield buffer.decode("utf-8", errors="replace")


def _parse_header(record: str) -> ParsedCommit | None:
    """Turn a header record into a :class:`ParsedCommit` with no files yet."""
    fields = record.split(FIELD_SEP)
    if len(fields) < 9:
        log.warning("malformed commit header with %d fields; skipping", len(fields))
        return None
    sha, parents, an, ae, ai, cn, ce, ci, subject = fields[:9]
    body = fields[9] if len(fields) > 9 else ""
    return ParsedCommit(
        sha=sha.strip(),
        parents=parents.split() if parents.strip() else [],
        author_name=an,
        author_email=ae.strip().lower(),
        authored_at=_parse_git_date(ai),
        committer_name=cn,
        committer_email=ce.strip().lower(),
        committed_at=_parse_git_date(ci),
        subject=subject,
        body=body,
    )


class _CommitAssembler:
    """Accumulates raw and numstat records for one commit, then merges them."""

    def __init__(self) -> None:
        # path -> (change_type, old_path, similarity)
        self.raw: dict[str, tuple[str, str | None, int | None]] = {}
        # path -> (insertions, deletions, is_binary)
        self.numstat: dict[str, tuple[int, int, bool]] = {}
        # Order of first appearance, so output is deterministic.
        self.order: list[str] = []
        self._seen: set[str] = set()
        self._pending_raw: tuple[str, int | None] | None = None
        self._pending_rename_old: str | None = None
        self._pending_numstat: tuple[int, int, bool] | None = None
        self._pending_numstat_old: str | None = None

    def _note(self, path: str) -> None:
        """Record first-appearance order for a path.

        Tracked in a separate set rather than by probing ``raw``/``numstat``,
        because callers insert into those dicts before calling here.
        """
        if path not in self._seen:
            self._seen.add(path)
            self.order.append(path)

    def feed(self, record: str) -> None:
        """Consume one record belonging to the current commit."""
        if not record:
            return

        # --- a path record following a raw status line ---
        #
        # Tested before the ":" sniff, not after. A record that arrives where a
        # path is due *is* a path, whatever its first byte -- and `:` is a legal
        # first character for a filename. Sniffing first ate `:zz.txt` as a
        # status line: the real file lost its status letter and its line counts,
        # and the numstat record that followed was consumed as a path, putting a
        # file called `1\t0\t1a.txt` into the corpus, co-occurring with every
        # real file in that commit.
        if self._pending_raw is not None:
            letter, similarity = self._pending_raw
            if letter in ("R", "C"):
                if self._pending_rename_old is None:
                    # First of two path records: the source path.
                    self._pending_rename_old = record
                    return
                new_path = record
                self.raw[new_path] = (letter, self._pending_rename_old, similarity)
                self._note(new_path)
                self._pending_raw = None
                self._pending_rename_old = None
                return
            self.raw[record] = (letter, None, similarity)
            self._note(record)
            self._pending_raw = None
            return

        # --- raw block: a ":<modes> <shas> <STATUS>" line ---
        if record.startswith(":"):
            status_field = record.rsplit(" ", 1)[-1].strip()
            letter = status_field[:1].upper() or "M"
            similarity = None
            if len(status_field) > 1 and status_field[1:].isdigit():
                similarity = int(status_field[1:])
            self._pending_raw = (letter, similarity)
            self._pending_rename_old = None
            return

        # --- numstat block: "<adds>\t<dels>\t<path>" ---
        parts = record.split("\t")
        if len(parts) >= 3:
            adds_s, dels_s, path = parts[0], parts[1], "\t".join(parts[2:])
            is_binary = adds_s == "-" or dels_s == "-"
            adds = 0 if is_binary else _safe_int(adds_s)
            dels = 0 if is_binary else _safe_int(dels_s)
            if path == "":
                # Rename/copy: the two following records are old and new paths.
                self._pending_numstat = (adds, dels, is_binary)
                self._pending_numstat_old = None
            else:
                self.numstat[path] = (adds, dels, is_binary)
                self._note(path)
            return

        # --- path records trailing a rename numstat header ---
        if self._pending_numstat is not None:
            if self._pending_numstat_old is None:
                self._pending_numstat_old = record
                return
            self.numstat[record] = self._pending_numstat
            self._note(record)
            self._pending_numstat = None
            self._pending_numstat_old = None
            return

    def build(self) -> list[FileChange]:
        """Merge the two blocks into one list of file changes."""
        changes: list[FileChange] = []
        for path in self.order:
            letter, old_path, similarity = self.raw.get(path, ("M", None, None))
            insertions, deletions, is_binary = self.numstat.get(path, (0, 0, False))
            changes.append(
                FileChange(
                    path=path,
                    change_type=letter,
                    insertions=insertions,
                    deletions=deletions,
                    is_binary=is_binary,
                    old_path=old_path,
                    similarity=similarity,
                )
            )
        return changes


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def iter_commits(
    mirror: Path,
    since_shas: list[str] | None = None,
    rev: str = "HEAD",
    include_tags: bool = False,
    include_merges: bool | None = None,
    rename_similarity: int | None = None,
    blobless: bool = False,
    reverse: bool = True,
) -> Iterator[ParsedCommit]:
    """Stream commits out of a bare mirror, newest first.

    Args:
        mirror: path to the bare repository.
        rev: what to walk. Defaults to HEAD, i.e. the default branch only.
        include_tags: also walk commits reachable from tags. A release is often
            cut on a branch that never merges back, so its commits are otherwise
            never read -- and the range between two releases is uncomputable.
            Coupling is a claim about the code that shipped, and 25.8% of this
            corpus exists solely on branches that never merged: abandoned
            experiments, and backports that restate a change already counted on
            the mainline. Walking those inflated co-change counts with work that
            was never released and, because a branch can delete a file the
            mainline still has, produced flatly false statements about HEAD.
        since_shas: exclude these commits and all their ancestors, giving an
            incremental read. Pass the previous run's tip, not
            the previous run's default-branch tip. The caller must have
            verified each SHA still
            exists -- a force-push can orphan one, and git errors on an unknown
            revision.
        include_merges: keep merge commits. Merges restate their parents'
            changes, so they are excluded by default.
        rename_similarity: git rename-detection threshold, as a percentage.
            Ignored for blobless mirrors, which are pinned to 100.
        blobless: the mirror has no blob objects. Drops ``--numstat`` and
            forces exact-only rename detection, because both line counting and
            inexact rename detection read file contents -- on a blobless mirror
            that triggers a promisor fetch that fails or hangs.
        reverse: emit oldest commits first. Required by the loader, whose
            rename tracking depends on having already seen a file's previous
            name by the time the move is reported.

    Yields:
        One :class:`ParsedCommit` per commit, in git log order. On a blobless
        mirror every ``FileChange`` reports zero insertions and deletions.

    Raises:
        GitError: if git exits non-zero.
    """
    import subprocess

    cfg = get_config().ingest
    # Default off, and nothing in the product turns it on: a merge restates the
    # changes of its parents, so counting one is counting the same edit twice.
    include_merges = bool(include_merges)
    rename_similarity = rename_similarity or cfg.rename_similarity

    args = [
        "log",
        f"--format={LOG_FORMAT}",
        "-z",
        "--raw",
        "--no-color",
        "--date-order",
    ]
    if blobless:
        # Exact renames only: git resolves these by comparing blob SHAs
        # recorded in the trees, without ever reading blob content.
        args.append("-M100%")
    else:
        args.append("--numstat")
        args.append(f"-M{rename_similarity}%")
    if not include_merges:
        args.append("--no-merges")
    if reverse:
        args.append("--reverse")
    # The revision goes after the options, not before them. Leading `--all` was
    # fine because it is an option; a bare revision that fails to resolve -- as
    # HEAD does in a repository with no commits -- is read as a path, and git
    # then rejects every option that follows it.
    args.append(rev)
    if include_tags:
        args.append("--tags")
    for sha in since_shas or []:
        args.append(f"^{sha}")

    proc = subprocess.Popen(
        ["git", *args],
        cwd=str(mirror),
        env=_base_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=_CHUNK,
    )

    current: ParsedCommit | None = None
    assembler = _CommitAssembler()
    # git emits one newline between a commit's --format output and its diff
    # block, and it lands as a prefix on the record that follows the header.
    # Exactly one, exactly there: `lstrip("\n")` on every record instead
    # rewrote any path whose own first character is a newline, and since the raw
    # and numstat blocks were then keyed on two different strings, one real file
    # became two rows -- one of them a path that has never existed.
    after_header = False
    try:
        for record in _iter_records(proc.stdout):
            if after_header and record.startswith("\n"):
                record = record[1:]
            after_header = False
            if record.startswith(COMMIT_SENTINEL):
                if current is not None:
                    current.files = assembler.build()
                    yield current
                assembler = _CommitAssembler()
                current = _parse_header(record[1:])
                after_header = True
                continue
            if current is not None:
                assembler.feed(record)

        if current is not None:
            current.files = assembler.build()
            yield current
    finally:
        if proc.stdout:
            proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if proc.stderr:
            proc.stderr.close()
        returncode = proc.wait()
        if returncode != 0:
            # A repository with no commits has no HEAD to resolve. `--all`
            # returned nothing and exited 0, so scoping the walk to the default
            # branch turned three empty repositories into hard failures. An
            # empty repository is a legitimate no-op, not an error.
            #
            # Falling off the end rather than returning: a `return` inside a
            # `finally` discards whatever exception was already propagating, so
            # a parse error in the loop above vanished whenever git also
            # reported an empty repository -- the run then looked like a
            # repository with no commits.
            lowered = stderr.lower()
            if "unknown revision" in lowered or "does not have any commits yet" in lowered:
                log.info("no commits reachable in %s; nothing to read", mirror)
            else:
                raise GitError(args, returncode, stderr)


def split_path(path: str) -> tuple[str, str, str | None, int]:
    """Decompose a repo-relative path into ``(dir, basename, extension, depth)``.

    Dotfiles are handled explicitly: ``.gitignore`` has basename ``.gitignore``
    and no extension, while ``.eslintrc.json`` correctly yields ``json``. Only a
    literal ``./`` prefix is stripped -- using ``lstrip("./")`` here would eat the
    leading dot of every dotfile.

    Extensions are lowercased and length-capped so a pathological filename
    cannot overflow the column.
    """
    normalised = path.strip().removeprefix("./")
    if "/" in normalised:
        dir_path, _, basename = normalised.rpartition("/")
    else:
        dir_path, basename = "", normalised

    # Ignore leading dots when looking for an extension, so a dotfile with no
    # further dots is treated as extensionless.
    stem = basename.lstrip(".")
    extension: str | None = None
    if "." in stem:
        candidate = stem.rpartition(".")[2].lower()
        if candidate and len(candidate) <= 20:
            extension = candidate

    depth = normalised.count("/")
    return dir_path, basename, extension, depth


