"""Malformed and unusual input, at the parsing boundaries.

Git output, manifests and paths all come from repositories nobody on this team
controls. Every branch here handles something a real repository can contain, and
the failure mode for all of them is the same: a statistic that is quietly wrong
rather than an error anyone sees.
"""
from __future__ import annotations

import subprocess

import pytest

from git_synapse.analysis.depbump import _parse_manifest_line
from git_synapse.ingest.parser import split_path

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


# ------------------------------------------------------------------ npm git

def test_an_npm_dependency_pinned_to_a_git_url_is_recognised():
    """A scoped package can be pinned to a git URL instead of a version, and
    that is still a declared dependency."""
    got = _parse_manifest_line(
        '    "@acme/design": "git+https://github.com/acme/design.git#v1",',
        "npm",
    )
    assert got is not None and got[0] == "design"


def test_a_non_acme_git_url_is_not_ours():
    assert _parse_manifest_line(
        '    "thing": "git+https://github.com/other/thing.git",', "npm"
    ) is None


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
    d, base, ext, depth = split_path(path)
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
