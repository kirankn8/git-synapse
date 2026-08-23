"""Commit parsing: the NUL-separated git log stream, decoded.

`commit_file` is the atomic fact the whole system rests on, so a parse error
here is not a crash -- it is a statistic that is quietly wrong.
"""
from __future__ import annotations

import subprocess

import pytest

from git_synapse.ingest.parser import iter_commits, split_path

ENV = {
    "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@example.com",
    "GIT_COMMITTER_NAME": "Bob", "GIT_COMMITTER_EMAIL": "bob@example.com",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _bare(tmp_path, steps):
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    for msg, action in steps:
        action(work)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
        subprocess.run(["git", "commit", "--quiet", "-m", msg], cwd=work, check=True, env=ENV)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)
    return bare


# ------------------------------------------------------------- split_path

@pytest.mark.parametrize(("path", "expected"), [
    ("a/b/c.go", ("a/b", "c.go", "go", 2)),
    ("top.md", ("", "top.md", "md", 0)),
    # A dotfile's leading dot is not an extension, and lstrip("./") once ate it.
    (".gitignore", ("", ".gitignore", None, 0)),
    (".github/workflows/ci.yml", (".github/workflows", "ci.yml", "yml", 2)),
    ("no_extension", ("", "no_extension", None, 0)),
    ("a/b/.hidden", ("a/b", ".hidden", None, 2)),
    ("archive.tar.gz", ("", "archive.tar.gz", "gz", 0)),
])
def test_split_path_components(path, expected):
    assert split_path(path) == expected


def test_split_path_survives_unicode_and_spaces():
    d, base, ext, depth = split_path("日本/テスト ファイル.go")
    assert d == "日本" and base == "テスト ファイル.go" and ext == "go" and depth == 1


# ------------------------------------------------------- real git streams

def test_parses_authors_committers_and_subjects(tmp_path):
    mirror = _bare(tmp_path, [("first change", lambda w: (w / "a.txt").write_text("1"))])
    commits = list(iter_commits(mirror))
    assert len(commits) == 1
    c = commits[0]
    assert c.subject == "first change"
    assert c.author_email == "ann@example.com"
    assert c.committer_email == "bob@example.com"
    assert len(c.sha) == 40
    assert [f.path for f in c.files] == ["a.txt"]


def test_change_types_are_recorded(tmp_path):
    def add(w): (w / "x.txt").write_text("one")
    def modify(w): (w / "x.txt").write_text("two")
    def delete(w): (w / "x.txt").unlink()

    mirror = _bare(tmp_path, [("add", add), ("modify", modify), ("delete", delete)])
    types = [f.change_type for c in iter_commits(mirror) for f in c.files]
    assert types == ["A", "M", "D"]


def test_a_rename_carries_its_old_path(tmp_path):
    def add(w): (w / "old.txt").write_text("x" * 200)
    def rename(w): (w / "old.txt").rename(w / "new.txt")

    mirror = _bare(tmp_path, [("add", add), ("rename", rename)])
    last = list(iter_commits(mirror))[-1]
    f = last.files[0]
    assert f.change_type == "R"
    assert f.old_path == "old.txt" and f.path == "new.txt"
    assert f.similarity is not None


def test_commits_arrive_oldest_first(tmp_path):
    steps = [(f"c{i}", (lambda i: lambda w: (w / f"f{i}.txt").write_text(str(i)))(i))
             for i in range(4)]
    subjects = [c.subject for c in iter_commits(_bare(tmp_path, steps))]
    assert subjects == ["c0", "c1", "c2", "c3"]


def test_since_shas_excludes_already_read_history(tmp_path):
    steps = [(f"c{i}", (lambda i: lambda w: (w / f"f{i}.txt").write_text(str(i)))(i))
             for i in range(4)]
    mirror = _bare(tmp_path, steps)
    everything = list(iter_commits(mirror))
    midpoint = everything[1].sha

    rest = list(iter_commits(mirror, since_shas=[midpoint]))
    assert [c.subject for c in rest] == ["c2", "c3"]


def test_a_commit_touching_many_files_keeps_them_all(tmp_path):
    def many(w):
        for i in range(25):
            (w / f"m{i}.txt").write_text(str(i))

    commits = list(iter_commits(_bare(tmp_path, [("bulk", many)])))
    assert len(commits[0].files) == 25
    assert len(commits[0].files) == 25


def test_unicode_paths_and_subjects_round_trip(tmp_path):
    def add(w):
        (w / "日本").mkdir()
        (w / "日本" / "テスト.go").write_text("x")

    c = list(iter_commits(_bare(tmp_path, [("añadir 日本 🎉", add)])))[0]
    assert c.subject == "añadir 日本 🎉"
    assert c.files[0].path == "日本/テスト.go"


def test_a_subject_containing_the_field_separator_does_not_corrupt_the_stream(tmp_path):
    """The parser splits on control characters; a subject containing one would
    desynchronise every commit after it."""
    def add(w): (w / "a.txt").write_text("x")

    c = list(iter_commits(_bare(tmp_path, [("weird | subject -- with $chars", add)])))[0]
    assert c.subject == "weird | subject -- with $chars"
    assert c.files[0].path == "a.txt"


def test_merge_commits_are_excluded_by_default(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    (work / "base.txt").write_text("b")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "checkout", "--quiet", "-b", "side"], cwd=work, check=True, env=ENV)
    (work / "side.txt").write_text("s")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "side"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "checkout", "--quiet", "main"], cwd=work, check=True, env=ENV)
    (work / "main.txt").write_text("m")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "main"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "merge", "--quiet", "--no-ff", "side", "-m", "merge side"],
                   cwd=work, check=True, env=ENV)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)

    subjects = [c.subject for c in iter_commits(bare)]
    assert "merge side" not in subjects
    assert all(not c.is_merge for c in iter_commits(bare))


def test_only_the_default_branch_is_walked(tmp_path):
    """A quarter of the real corpus lived on branches that never merged."""
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    (work / "a.txt").write_text("a")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "on main"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "checkout", "--quiet", "-b", "abandoned"], cwd=work, check=True, env=ENV)
    (work / "b.txt").write_text("b")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "never merged"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "checkout", "--quiet", "main"], cwd=work, check=True, env=ENV)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)

    subjects = [c.subject for c in iter_commits(bare)]
    assert subjects == ["on main"], subjects
