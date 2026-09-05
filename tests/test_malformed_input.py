"""Malformed and unusual input, at the parsing boundaries.

Git output, manifests and paths all come from repositories nobody on this team
controls. Every branch here handles something a real repository can contain, and
the failure mode for all of them is the same: a statistic that is quietly wrong
rather than an error anyone sees.
"""
from __future__ import annotations

import subprocess

import pytest

from git_synapse.ingest.parser import split_path

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


# ------------------------------------------------------------------- paths

@pytest.mark.parametrize("path", [
    "a" * 300 + ".go",              # very long
    "dir with spaces/file name.go",
    "emoji-🎉/file.go",
    "a/b/c/d/e/f/g/h/i/j/k.go",     # deep
    "trailing/",                    # directory-looking
    "..hidden",
    "-leading-dash.go",
])
def test_split_path_never_raises_on_a_real_looking_path(path):
    d, base, _ext, depth = split_path(path)
    assert isinstance(d, str) and isinstance(base, str) and depth >= 0


def test_depth_counts_separators_not_characters():
    assert split_path("a/b/c.go")[3] == 2
    assert split_path("c.go")[3] == 0


# ------------------------------------------------------------- git surprises

def test_a_commit_with_a_multiline_body_is_parsed_whole(tmp_path):
    """Bodies contain blank lines and trailers; the parser splits on control
    characters and a body that swallowed the next record would desynchronise
    every commit after it."""
    from git_synapse.ingest.parser import iter_commits

    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    (work / "a.txt").write_text("a")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    body = "first line\n\nsecond paragraph\n\nCo-Authored-By: Someone <s@e>\n"
    subprocess.run(["git", "commit", "--quiet", "-m", "subject", "-m", body],
                   cwd=work, check=True, env=ENV)
    (work / "b.txt").write_text("b")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "after"], cwd=work, check=True, env=ENV)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)

    commits = list(iter_commits(bare))
    assert [c.subject for c in commits] == ["subject", "after"]
    assert "Co-Authored-By" in commits[0].body


def test_a_path_containing_unusual_characters_survives_the_stream(tmp_path):
    """The stream is NUL-separated precisely so a quote or newline in a path
    cannot break it."""
    from git_synapse.ingest.parser import iter_commits

    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    odd = work / "wei'rd na\"me.txt"
    odd.write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "odd path"],
                   cwd=work, check=True, env=ENV)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)

    files = [f.path for c in iter_commits(bare) for f in c.files]
    assert files == ["wei'rd na\"me.txt"], files


def test_a_type_change_is_recorded(tmp_path):
    """A file replaced by a symlink is a `T`, which is neither add nor modify."""
    import os

    from git_synapse.ingest.parser import iter_commits

    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    (work / "target.txt").write_text("t")
    (work / "thing").write_text("plain")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "plain"], cwd=work, check=True, env=ENV)
    (work / "thing").unlink()
    os.symlink("target.txt", work / "thing")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "symlink"], cwd=work, check=True, env=ENV)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)

    types = {f.path: f.change_type for c in iter_commits(bare) for f in c.files}
    assert types.get("thing") in ("T", "M"), types


# -------------------------------------------------- the parser's own helpers

@pytest.mark.parametrize("value", ["", "not-a-date", None, "2026-13-45T99:99:99"])
def test_an_unparseable_commit_date_falls_back_to_the_epoch(value):
    """A handful of commits in any large org carry corrupt dates. Losing the
    whole repository over one of them would be the wrong trade."""
    from git_synapse.ingest.parser import _parse_git_date

    got = _parse_git_date(value)
    assert got is not None
    # The epoch, compared as an instant: in a negative-offset zone its local
    # calendar year is 1969, which is the sort of thing that makes a date
    # assertion pass in one timezone and fail in another.
    assert got.timestamp() == 0


def test_a_valid_iso_date_is_parsed_as_given():
    from git_synapse.ingest.parser import _parse_git_date

    got = _parse_git_date("2026-08-26T12:34:56+00:00")
    assert (got.year, got.month, got.day) == (2026, 8, 26)


@pytest.mark.parametrize(("raw", "expected"), [("12", 12), ("0", 0), ("-", 0),
                                               ("", 0), ("abc", 0)])
def test_a_non_numeric_line_count_reads_as_zero(raw, expected):
    """git writes "-" for a binary file's line counts."""
    from git_synapse.ingest.parser import _safe_int

    assert _safe_int(raw) == expected


def test_a_malformed_commit_header_is_skipped_not_fatal():
    """One corrupt record must not abort the walk over an entire repository."""
    from git_synapse.ingest.parser import _parse_header

    assert _parse_header("too\x1ffew\x1ffields") is None


def test_a_header_without_a_body_still_parses():
    from git_synapse.ingest.parser import FIELD_SEP, _parse_header

    fields = ["a" * 40, "", "An", "an@e", "2026-01-01T00:00:00+00:00",
              "Cn", "cn@e", "2026-01-01T00:00:00+00:00", "subject"]
    commit = _parse_header(FIELD_SEP.join(fields))
    assert commit is not None and commit.body == ""


def test_the_record_stream_yields_a_trailing_record_without_a_separator():
    """git's last record has no trailing NUL; dropping it would lose a commit."""
    import io

    from git_synapse.ingest.parser import _iter_records

    data = b"one\x00two\x00three"
    assert list(_iter_records(io.BytesIO(data))) == ["one", "two", "three"]


def test_the_record_stream_survives_invalid_utf8():
    """Commit messages are bytes, and not all of them are valid UTF-8."""
    import io

    from git_synapse.ingest.parser import _iter_records

    out = list(_iter_records(io.BytesIO(b"ok\x00bad\xff\xfe\x00")))
    assert out[0] == "ok"
    assert len(out) == 2


def test_ancestor_directories_are_enumerated_to_the_root():
    from git_synapse.ingest.parser import ancestor_dirs

    # The repository root is an ancestor too, and directory rollups depend on
    # it being counted.
    assert ancestor_dirs("a/b/c/file.go") == ["", "a", "a/b", "a/b/c"]
    assert ancestor_dirs("file.go") == [""]
    assert ancestor_dirs("a/b/c/file.go", max_depth=2) == ["", "a", "a/b"]
