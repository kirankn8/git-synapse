"""Streaming parser for ``git log`` output."""

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
    """Parse git's strict-ISO ``%aI`` output."""
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
        """Record first-appearance order for a path."""
        if path not in self._seen:
            self._seen.add(path)
            self.order.append(path)

    def feed(self, record: str) -> None:
        """Consume one record belonging to the current commit."""
        if not record:
            return

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

        if record.startswith(":"):
            status_field = record.rsplit(" ", 1)[-1].strip()
            letter = status_field[:1].upper() or "M"
            similarity = None
            if len(status_field) > 1 and status_field[1:].isdigit():
                similarity = int(status_field[1:])
            self._pending_raw = (letter, similarity)
            self._pending_rename_old = None
            return

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
    """Stream commits out of a bare mirror, newest first."""
    import subprocess

    cfg = get_config().ingest
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
        args.append("-M100%")
    else:
        args.append("--numstat")
        args.append(f"-M{rename_similarity}%")
    if not include_merges:
        args.append("--no-merges")
    if reverse:
        args.append("--reverse")
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
            lowered = stderr.lower()
            if "unknown revision" in lowered or "does not have any commits yet" in lowered:
                log.info("no commits reachable in %s; nothing to read", mirror)
            else:
                raise GitError(args, returncode, stderr)


def split_path(path: str) -> tuple[str, str, str | None, int]:
    """Decompose a repo-relative path into ``(dir, basename, extension, depth)``."""
    normalised = path.strip().removeprefix("./")
    if "/" in normalised:
        dir_path, _, basename = normalised.rpartition("/")
    else:
        dir_path, basename = "", normalised

    stem = basename.lstrip(".")
    extension: str | None = None
    if "." in stem:
        candidate = stem.rpartition(".")[2].lower()
        if candidate and len(candidate) <= 20:
            extension = candidate

    depth = normalised.count("/")
    return dir_path, basename, extension, depth


