"""Mirror management, driven against real local repositories.

This is where the worst incident happened: an expired token made every fetch
fail, the fallback re-cloned, and the clone deleted the existing mirror before
failing on the same error -- 213 of 272 working mirrors destroyed in one run.
Everything here exists to keep that shape impossible.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from git_synapse.ingest import gitops
from git_synapse.ingest.gitops import (
    GitError,
    choose_clone_mode,
    clone_mirror,
    commit_exists,
    current_head,
    default_branch,
    fetch_mirror,
    is_permanent_error,
    is_transient_error,
    is_valid_mirror,
    mirror_is_blobless,
    ref_tips,
    repo_size_kb,
    sync_mirror,
)

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


@pytest.fixture
def remote(tmp_path):
    """A bare repo standing in for GitHub."""
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=ENV)
    for i in range(3):
        (work / f"f{i}.txt").write_text(str(i))
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
        subprocess.run(["git", "commit", "--quiet", "-m", f"c{i}"], cwd=work, check=True, env=ENV)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=ENV)
    return work, bare


# ------------------------------------------------------ error classification

@pytest.mark.parametrize("stderr", [
    "remote: Invalid username or token.",
    "fatal: Authentication failed for 'https://github.com/x/y.git/'",
    "ERROR: Repository not found.",
    "remote: Permission denied",
    "The requested URL returned error: 403 Forbidden",
    "The requested URL returned error: 401 Unauthorized",
])
def test_credential_and_missing_repo_failures_are_permanent(stderr):
    """Re-cloning on these deletes a working mirror and then fails identically."""
    assert is_permanent_error(stderr)
    assert not is_transient_error(stderr)


@pytest.mark.parametrize("stderr", [
    "fatal: unable to access: Failed to connect to github.com port 443",
    "error: RPC failed; curl 56 Recv failure",
    "fatal: the remote end hung up unexpectedly",
    "The requested URL returned error: 502 Bad Gateway",
    "gnutls_handshake() failed",
    "Operation timed out",
])
def test_network_failures_are_transient(stderr):
    """A network failure says nothing about the mirror, which is still good."""
    assert is_transient_error(stderr)
    assert not is_permanent_error(stderr)


@pytest.mark.parametrize("stderr", ["", "fatal: not a git repository", "some unknown error"])
def test_an_unclassified_failure_is_neither(stderr):
    """Only these reach the re-clone path, because only they implicate the mirror."""
    assert not is_permanent_error(stderr)
    assert not is_transient_error(stderr)


# ------------------------------------------------------------ clone and sync

def test_clone_produces_a_valid_bare_mirror(tmp_path, remote):
    _, bare = remote
    dest = tmp_path / "mirror.git"
    clone_mirror(str(bare), dest, blobless=False)
    assert is_valid_mirror(dest)
    assert default_branch(dest) in ("main", "master")
    assert current_head(dest)
    assert repo_size_kb(dest) >= 0
    assert not mirror_is_blobless(dest)


def test_a_failed_clone_leaves_no_half_written_mirror(tmp_path):
    """The staging-then-swap exists so a failure cannot leave a broken mirror."""
    dest = tmp_path / "mirror.git"
    with pytest.raises(GitError):
        clone_mirror(str(tmp_path / "nonexistent.git"), dest, blobless=False)
    assert not dest.exists()
    assert not (tmp_path / "mirror.git.incoming").exists()


def test_a_failed_reclone_preserves_the_existing_mirror(tmp_path, remote):
    """The 213-mirror incident: the clone deleted first, then failed."""
    _, bare = remote
    dest = tmp_path / "mirror.git"
    clone_mirror(str(bare), dest, blobless=False)
    before = current_head(dest)

    with pytest.raises(GitError):
        clone_mirror(str(tmp_path / "gone.git"), dest, blobless=False)

    assert is_valid_mirror(dest), "the existing mirror was destroyed by a failed clone"
    assert current_head(dest) == before


def test_fetch_reports_whether_anything_moved(tmp_path, remote):
    work, bare = remote
    dest = tmp_path / "mirror.git"
    clone_mirror(str(bare), dest, blobless=False)

    assert fetch_mirror(dest, str(bare), blobless=False) is False, "nothing changed yet"

    (work / "new.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "commit", "--quiet", "-m", "new"], cwd=work, check=True, env=ENV)
    subprocess.run(["git", "push", "--quiet", str(bare), "main"], cwd=work, check=True, env=ENV)

    assert fetch_mirror(dest, str(bare), blobless=False) is True


def test_sync_clones_then_fetches(tmp_path, remote, monkeypatch):
    _, bare = remote
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    from git_synapse.config import reset_config_cache

    reset_config_cache()
    try:
        first = sync_mirror("t/demo", str(bare), blobless=False)
        assert first.cloned is True
        second = sync_mirror("t/demo", str(bare), blobless=False)
        assert second.cloned is False, "an existing mirror must be fetched, not re-cloned"
        assert second.head_sha == first.head_sha
    finally:
        reset_config_cache()


def test_ref_tips_returns_the_default_branch_tip(tmp_path, remote):
    _, bare = remote
    dest = tmp_path / "mirror.git"
    clone_mirror(str(bare), dest, blobless=False)
    tips = ref_tips(dest)
    assert tips == [current_head(dest)]


def test_commit_exists_distinguishes_present_from_absent(tmp_path, remote):
    _, bare = remote
    dest = tmp_path / "mirror.git"
    clone_mirror(str(bare), dest, blobless=False)
    assert commit_exists(dest, current_head(dest))
    assert not commit_exists(dest, "0" * 40)


def test_an_empty_directory_is_not_a_valid_mirror(tmp_path):
    empty = tmp_path / "empty.git"
    empty.mkdir()
    assert not is_valid_mirror(empty)
    assert not is_valid_mirror(tmp_path / "does-not-exist")


# ---------------------------------------------------------------- clone mode

@pytest.mark.parametrize(("size_kb", "expected"), [
    (None, False),          # unknown size: take the safe, complete clone
    (0, False),
    (1_000, False),         # small repo: full clone, so churn data exists
    (50_000_000, True),     # very large: blobless, or the disk is gone
])
def test_clone_mode_follows_size(size_kb, expected):
    assert choose_clone_mode(size_kb) is expected


def test_mirror_path_is_namespaced_by_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path))
    from git_synapse.config import reset_config_cache

    reset_config_cache()
    try:
        p = gitops.mirror_path_for("owner/name")
        assert p.name == "name.git"
        assert p.parent.name == "owner"
    finally:
        reset_config_cache()


# --------------------------------------------- sync's response to failure

def test_a_permanent_failure_preserves_the_mirror_and_raises(tmp_path, remote, monkeypatch):
    """An expired token made every fetch fail; the fallback re-cloned, and the
    clone deleted the mirror before failing on the same error."""
    _, bare = remote
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    from git_synapse.config import reset_config_cache

    reset_config_cache()
    try:
        first = sync_mirror("t/perm", str(bare), blobless=False)
        assert first.cloned

        def auth_failure(*a, **kw):
            raise GitError(["fetch"], 128, "remote: Invalid username or token.")

        monkeypatch.setattr(gitops, "fetch_mirror", auth_failure)
        cloned = []
        monkeypatch.setattr(gitops, "clone_mirror",
                            lambda *a, **kw: cloned.append(a) or None)

        with pytest.raises(GitError):
            sync_mirror("t/perm", str(bare), blobless=False)
        assert not cloned, "a credential failure must never trigger a re-clone"
        assert is_valid_mirror(gitops.mirror_path_for("t/perm"))
    finally:
        reset_config_cache()


def test_a_transient_failure_preserves_the_mirror_and_raises(tmp_path, remote, monkeypatch):
    """A network blip says nothing about the mirror; re-cloning spent nine
    minutes per repository failing to replace one that was fine."""
    _, bare = remote
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    from git_synapse.config import reset_config_cache

    reset_config_cache()
    try:
        sync_mirror("t/trans", str(bare), blobless=False)

        def outage(*a, **kw):
            raise GitError(["fetch"], 128,
                           "fatal: unable to access: Failed to connect to github.com port 443")

        monkeypatch.setattr(gitops, "fetch_mirror", outage)
        cloned = []
        monkeypatch.setattr(gitops, "clone_mirror",
                            lambda *a, **kw: cloned.append(a) or None)

        with pytest.raises(GitError):
            sync_mirror("t/trans", str(bare), blobless=False)
        assert not cloned, "a network failure must never trigger a re-clone"
        assert is_valid_mirror(gitops.mirror_path_for("t/trans"))
    finally:
        reset_config_cache()


def test_a_damaged_mirror_is_the_one_case_that_does_re_clone(tmp_path, remote, monkeypatch):
    """Only a cause that actually implicates the mirror may reach that path."""
    _, bare = remote
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    from git_synapse.config import reset_config_cache

    reset_config_cache()
    try:
        sync_mirror("t/damaged", str(bare), blobless=False)

        def corrupt(*a, **kw):
            raise GitError(["fetch"], 128, "fatal: not a git repository")

        monkeypatch.setattr(gitops, "fetch_mirror", corrupt)
        cloned = []

        def fake_clone(*a, **kw):
            cloned.append(a)

        monkeypatch.setattr(gitops, "clone_mirror", fake_clone)
        sync_mirror("t/damaged", str(bare), blobless=False)
        assert cloned, "a damaged mirror must be re-cloned"
    finally:
        reset_config_cache()


def test_a_changed_clone_mode_forces_a_re_clone(tmp_path, remote, monkeypatch):
    """Serving mismatched data is worse than paying for a clone."""
    _, bare = remote
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    from git_synapse.config import reset_config_cache

    reset_config_cache()
    try:
        sync_mirror("t/mode", str(bare), blobless=False)
        result = sync_mirror("t/mode", str(bare), blobless=True)
        assert result.cloned, "switching to blobless must re-clone"
        assert mirror_is_blobless(gitops.mirror_path_for("t/mode"))
    finally:
        reset_config_cache()


def test_remove_mirror_reports_whether_there_was_one(tmp_path, remote, monkeypatch):
    _, bare = remote
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    from git_synapse.config import reset_config_cache

    reset_config_cache()
    try:
        sync_mirror("t/gone", str(bare), blobless=False)
        assert gitops.remove_mirror("t/gone") is True
        assert gitops.remove_mirror("t/gone") is False
    finally:
        reset_config_cache()
