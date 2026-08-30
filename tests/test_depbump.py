"""Manifest parsing and dependency extraction.

This layer produces the `declared` evidence tier, which agents are told to trust
above everything else, so a parse error here is the most expensive kind: it does
not look like a failure, it looks like a fact.
"""
from __future__ import annotations

import subprocess

import pytest

from git_synapse.analysis import depbump
from git_synapse.analysis.manifests import _PSEUDO
from git_synapse.analysis.depbump import (
    declared_at_head,
    declared_modules_at_head,
    extract_from_mirror,
    manifest_paths,
    repo_ref,
    resolve_repo,
)



# --------------------------------------------------------- pseudo-versions

@pytest.mark.parametrize(
    ("version", "sha"),
    [
        ("v0.0.0-20260626221153-5fc63d6f3055", "5fc63d6f3055"),
        ("v1.2.3-0.20260626221153-5fc63d6f3055", "5fc63d6f3055"),
        ("v1.2.3-pre.0.20260626221153-5fc63d6f3055", "5fc63d6f3055"),
    ],
)
def test_pseudo_version_shapes_all_yield_their_sha(version, sha):
    """The separator before the timestamp is '-' in one shape and '.' in the
    others; accepting only '-' silently dropped 17% of edges."""
    m = _PSEUDO.search(version)
    assert m is not None and m.group(2) == sha


@pytest.mark.parametrize("version", ["v1.2.3", "", "v0.0.0-2026-5fc63d6f3055", "latest"])
def test_release_versions_carry_no_sha(version):
    assert _PSEUDO.search(version) is None


# ------------------------------------------------- manifests on a real repo

def _repo(tmp_path, files: dict[str, str]):
    """A real bare mirror containing `files`, so the git-facing code is exercised."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", str(work)], check=True)
    for rel, body in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=env)
    subprocess.run(["git", "commit", "--quiet", "-m", "init"], cwd=work, check=True, env=env)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)], check=True)
    return bare


def test_manifest_paths_finds_nested_manifests_and_skips_vendored_ones(tmp_path):
    """Reading only the root manifest hid 371 internal references."""
    mirror = _repo(tmp_path, {
        "go.mod": "module github.com/acme/top\n",
        "svc/api/go.mod": "module github.com/acme/top/svc/api\n",
        "ui/package.json": '{"dependencies":{"@acme/design":"1.0.0"}}',
        "vendor/x/go.mod": "module vendored\n",
        "node_modules/y/package.json": '{"name":"y"}',
        "testdata/go.mod": "module fixture\n",
    })
    found = {p for p, _ in manifest_paths(mirror)}
    assert "go.mod" in found
    assert "svc/api/go.mod" in found
    assert "ui/package.json" in found
    # Vendored and fixture trees are other people's dependencies, not ours.
    assert not any(x.startswith(("vendor/", "node_modules/", "testdata/")) for x in found)


def test_manifest_paths_on_a_repo_with_no_manifests(tmp_path):
    assert manifest_paths(_repo(tmp_path, {"README.md": "hi\n"})) == []


def test_declared_at_head_excludes_the_module_itself(tmp_path):
    """A module requiring its own path is not a dependency on another repo."""
    mirror = _repo(tmp_path, {
        "go.mod": (
            "module github.com/acme/runtime\n\n"
            "require (\n"
            "\tgithub.com/acme/contracts v1.2.3\n"
            "\tgithub.com/acme/runtime/api v0.1.0\n"
            "\tgithub.com/other/lib v9.9.9\n"
            ")\n"
        ),
    })
    got = dict(declared_at_head(mirror, "runtime", "go.mod", "go"))
    # Stored as written, so the owner can be checked at resolution time; the
    # third-party reference is kept, and resolves to nothing until that
    # repository is onboarded.
    assert got == {
        "github.com/acme/contracts": "v1.2.3",
        "github.com/acme/runtime/api": "v0.1.0",
        "github.com/other/lib": "v9.9.9",
    }


def test_declared_at_head_on_a_missing_manifest_is_empty_not_an_error(tmp_path):
    mirror = _repo(tmp_path, {"README.md": "x\n"})
    assert declared_at_head(mirror, "any", "go.mod", "go") == []


# -------------------------------------------------- the intra-repo module graph

def _module_edges(mirror, repo_name):
    """Every internal edge across every manifest, the way refresh_modules does."""
    from git_synapse.analysis.depbump import declared_modules_at_head

    edges = set()
    for manifest, _ in manifest_paths(mirror):
        for consumer, dep, _version in declared_modules_at_head(mirror, repo_name, manifest):
            edges.add((consumer, dep))
    return edges


def test_declared_modules_at_head_maps_a_monorepos_internal_edges(tmp_path):
    """A monorepo's real structure is in its own submodules, and reading only
    the root manifest made that invisible."""
    mirror = _repo(tmp_path, {
        "go.mod": "module github.com/acme/mono\n",
        "svc/api/go.mod": (
            "module github.com/acme/mono/svc/api\n\n"
            "require github.com/acme/mono/pkg/core v0.0.0\n"
        ),
        "pkg/core/go.mod": "module github.com/acme/mono/pkg/core\n",
        "svc/web/go.mod": (
            "module github.com/acme/mono/svc/web\n\n"
            "require (\n"
            "\tgithub.com/acme/mono/pkg/core v0.0.0\n"
            "\tgithub.com/acme/mono/svc/api v0.0.0\n"
            ")\n"
        ),
    })
    pairs = _module_edges(mirror, "mono")
    assert ("svc/api", "pkg/core") in pairs
    assert ("svc/web", "pkg/core") in pairs
    assert ("svc/web", "svc/api") in pairs
    assert not any(a == b for a, b in pairs), "a module never declares itself"


def test_a_single_module_repo_has_no_internal_edges(tmp_path):
    mirror = _repo(tmp_path, {
        "go.mod": (
            "module github.com/acme/solo\n\n"
            "require github.com/acme/other v1.0.0\n"
        ),
    })
    assert _module_edges(mirror, "solo") == set()


def test_a_dependency_on_another_repository_is_not_an_internal_module_edge(tmp_path):
    """Cross-repo edges belong to repo_dependency, not module_dependency."""
    mirror = _repo(tmp_path, {
        "go.mod": "module github.com/acme/mono\n",
        "svc/api/go.mod": (
            "module github.com/acme/mono/svc/api\n\n"
            "require github.com/acme/elsewhere/pkg v1.0.0\n"
        ),
    })
    assert _module_edges(mirror, "mono") == set()


# ---------------------------------------------------------- history walking

def test_extract_from_mirror_finds_every_bump_in_history(tmp_path):
    """Bump history is the ground truth propagation lag rests on."""
    import subprocess

    from git_synapse.analysis.depbump import extract_from_mirror

    work = tmp_path / "w"
    work.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env=env)
    for i in range(4):
        (work / "go.mod").write_text(
            "module github.com/acme/consumer\n\n"
            "require github.com/acme/upstream "
            f"v0.0.0-2026010100000{i}-abcdef01234{i}\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=env)
        subprocess.run(["git", "commit", "--quiet", "-m", f"bump {i}"],
                       cwd=work, check=True, env=env)
    bare = tmp_path / "m.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)],
                   check=True, env=env)

    bumps = list(extract_from_mirror(bare, "consumer", "go.mod", "go"))
    assert len(bumps) >= 4, f"four bumps in history, found {len(bumps)}"
    assert all(b.dep_name == "github.com/acme/upstream" for b in bumps)
    # Each pseudo-version carries the upstream commit it pinned.
    assert all(b.dep_sha for b in bumps)


# ------------------------------------------------- the parser's darker corners



def test_a_blank_line_in_the_tree_listing_is_not_a_manifest(tmp_path, monkeypatch):
    """git can emit an empty line; treating it as a path would make its basename
    the empty string and index MANIFESTS with it."""
    import subprocess as sp

    class _Proc:
        returncode = 0
        stdout = "\n\ngo.mod\n\n"

    monkeypatch.setattr(sp, "run", lambda *a, **k: _Proc())
    assert manifest_paths(tmp_path) == [("go.mod", "go")]


def test_a_repository_that_is_not_a_git_directory_yields_no_manifests(tmp_path):
    empty = tmp_path / "notgit"
    empty.mkdir()
    assert manifest_paths(empty) == []


def test_a_manifest_scan_on_a_non_repository_is_empty_not_an_error(tmp_path):
    empty = tmp_path / "notgit"
    empty.mkdir()
    assert extract_from_mirror(empty, "anything") == []


def test_declared_at_head_skips_the_repositorys_own_module_line(tmp_path):
    mirror = _repo(tmp_path, {"go.mod": (
        "module github.com/acme/httpkit\n"
        "require (\n"
        "\tgithub.com/acme/httpkit v1.0.0\n"
        "\tgithub.com/acme/telemetry v2.0.0\n"
        ")\n"
    )})
    names = [n for n, _ in declared_at_head(mirror, "httpkit", "go.mod", "go")]
    assert names == ["github.com/acme/telemetry"], "the module's own name is not a dependency"


def test_declared_modules_on_a_non_repository_is_empty(tmp_path):
    empty = tmp_path / "notgit"
    empty.mkdir()
    assert declared_modules_at_head(empty, "anything", "go.mod") == []


def test_a_module_replacing_itself_is_not_an_internal_edge(tmp_path):
    """`replace` pointing a module at its own directory is a build directive, not
    a dependency; counting it would make every module couple to itself."""
    mirror = _repo(tmp_path, {
        "gateway/go.mod": (
            "module github.com/acme/mono/gateway\n"
            "require github.com/acme/mono/gateway v0.0.0\n"
            "require github.com/acme/mono/core v1.1.0\n"
        ),
    })
    edges = declared_modules_at_head(mirror, "mono", "gateway/go.mod")
    assert [dep for _, dep, _ in edges] == ["core"]


def test_a_module_line_is_never_read_as_a_dependency_even_when_it_parses(tmp_path):
    """A trailing comment gives the `module` line a token where a version would
    be, so the path regex matches it. Only the explicit `module ` check stops the
    repository from declaring a dependency on itself under a different name."""
    mirror = _repo(tmp_path, {
        "cmd/go.mod": (
            "module github.com/acme/mono/gateway // moved, kept for tooling\n"
            "require github.com/acme/mono/core v1.1.0\n"
        ),
    })
    edges = declared_modules_at_head(mirror, "mono", "cmd/go.mod")
    assert [dep for _, dep, _ in edges] == ["core"]


def test_the_manifest_cap_stops_a_vendored_tree_that_slipped_the_filter(tmp_path,
                                                                       monkeypatch):
    """A repository with tens of thousands of go.mod files under an unfiltered
    path would otherwise spawn a git invocation per manifest."""
    import subprocess as sp

    from git_synapse.analysis.depbump import MAX_MANIFESTS_PER_REPO

    class _Proc:
        returncode = 0
        stdout = "\n".join(f"pkg{i}/go.mod" for i in range(MAX_MANIFESTS_PER_REPO + 50))

    monkeypatch.setattr(sp, "run", lambda *a, **k: _Proc())
    assert len(manifest_paths(tmp_path)) == MAX_MANIFESTS_PER_REPO


# ------------------------------------------------------- resolving a reference

@pytest.mark.parametrize(("ref", "expected"), [
    ("github.com/acme/signer", ("acme", "signer")),
    ("github.com/acme/signer/v3", ("acme", "signer")),   # the Go major suffix
    ("https://gitlab.com/acme/signer.git", ("acme", "signer")),
    ("@acme/ui", ("acme", "ui")),
    ("acme/signer", ("acme", "signer")),
    ("serde", (None, "serde")),
])
def test_a_reference_splits_into_owner_and_name(ref, expected):
    assert repo_ref(ref) == expected


def test_a_different_owners_repository_of_the_same_name_is_not_a_match():
    """Matching on the name alone would let an unrelated company's library
    become an edge into this codebase."""
    by_full, by_name = {("acme", "utils"): 7}, {"utils": 7}
    assert resolve_repo("github.com/acme/utils", by_full, by_name) == 7
    assert resolve_repo("gitlab.com/otherco/utils", by_full, by_name) is None


def test_a_reference_with_no_owner_falls_back_to_the_name():
    """A bare crate or unscoped package carries no owner to check."""
    assert resolve_repo("utils", {("acme", "utils"): 7}, {"utils": 7}) == 7


def test_an_unknown_reference_resolves_to_nothing():
    assert resolve_repo("nope", {}, {}) is None


def test_documentation_is_not_scanned_for_dependencies(tmp_path, monkeypatch):
    """django ships `docs/ref/models/constraints.txt`, which is prose. Reading it
    as a pip constraints file invented dependencies called `name`."""
    import subprocess as sp
    from git_synapse.analysis import depbump

    class _Proc:
        returncode = 0
        stdout = "requirements.txt\ndocs/ref/models/constraints.txt\ndoc/x/requirements.txt\n"
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda *a, **k: _Proc())
    found = [p for p, _ in depbump.manifest_paths(tmp_path)]
    assert found == ["requirements.txt"]


# ------------------------------------------------------- resolution tiers
#
# These build the rows directly rather than ingesting a repository, because what
# is under test is the *matching* -- a declared version against a tag name --
# and a real repository would only obscure which spelling each case exercises.


@pytest.fixture
def bump_env(db):
    """An open transaction holding two repositories, rolled back afterwards."""
    from git_synapse.db.engine import connection

    with connection() as conn:
        repo = conn.execute(
            "INSERT INTO repo (full_name, name, owner) VALUES "
            "('acme/consumer','consumer','acme') RETURNING id").fetchone()[0]
        dep = conn.execute(
            "INSERT INTO repo (full_name, name, owner) VALUES "
            "('acme/library','library','acme') RETURNING id").fetchone()[0]
        yield conn, repo, dep
        conn.rollback()


def _commit(conn, repo_id, sha, when):
    return conn.execute(
        "INSERT INTO commit (repo_id, sha, authored_at, committed_at) "
        "VALUES (%s, %s, %s, %s) RETURNING id", (repo_id, sha, when, when)).fetchone()[0]


def _tag(conn, repo_id, name, commit_id, main_commit_id, key, at="2024-01-01"):
    conn.execute(
        "INSERT INTO ref_tag (repo_id, name, commit_sha, tagged_at, annotated, "
        "commit_id, main_commit_id, version_key) "
        "VALUES (%s, %s, %s, %s, FALSE, %s, %s, %s)",
        (repo_id, name, "0" * 40, at, commit_id, main_commit_id, key))


def _bump(conn, repo_id, dep_repo_id, version, at):
    sha = f"{abs(hash((version, at))):040x}"[:40]
    _commit(conn, repo_id, sha, at)
    return conn.execute(
        "INSERT INTO dep_bump (consumer_repo_id, consumer_sha, dep_repo_id, "
        "dep_name, dep_version, manifest, bumped_at) "
        "VALUES (%s, %s, %s, 'library', %s, 'pom.xml', %s) RETURNING id",
        (repo_id, sha, dep_repo_id, version, at)).fetchone()[0]


def _resolved(conn, bump_id):
    return tuple(conn.execute(
        "SELECT dep_commit_id, resolution FROM dep_bump WHERE id = %s",
        (bump_id,)).fetchone())


def test_a_declared_version_resolves_through_the_shipping_branch_anchor(bump_env):
    """The whole point of the anchor: guava tags on a release branch, so the
    tagged commit was never walked and only the merge-base exists."""
    conn, repo, dep = bump_env
    anchor = _commit(conn, dep, "aa" * 20, "2024-01-01")
    _tag(conn, dep, "v33.4.0", commit_id=None, main_commit_id=anchor, key="33.4")
    row = _bump(conn, repo, dep, version="33.4.0-jre", at="2024-02-01")

    depbump.resolve_bumps(conn)
    assert _resolved(conn, row) == (anchor, "tag")


def test_a_range_resolves_to_its_declared_floor(bump_env):
    """`^4.17.21` states 4.17.21 as its own lower bound, so that is the version
    taken -- and it is recorded as a floor, not as an exact answer."""
    conn, repo, dep = bump_env
    c = _commit(conn, dep, "bb" * 20, "2024-01-01")
    _tag(conn, dep, "v4.17.21", commit_id=c, main_commit_id=c, key="4.17.21")
    row = _bump(conn, repo, dep, version="^4.17.21", at="2024-02-01")

    depbump.resolve_bumps(conn)
    assert _resolved(conn, row) == (c, "floor")


def test_an_upper_bound_resolves_to_the_newest_release_beneath_it(bump_env):
    """`<3.0` names no version that was used, so the answer is the newest one
    that existed and was permitted."""
    conn, repo, dep = bump_env
    old = _commit(conn, dep, "c1" * 20, "2023-01-01")
    new = _commit(conn, dep, "c2" * 20, "2023-06-01")
    over = _commit(conn, dep, "c3" * 20, "2023-07-01")
    _tag(conn, dep, "v2.9", commit_id=old, main_commit_id=old, key="2.9", at="2023-01-02")
    _tag(conn, dep, "v2.10", commit_id=new, main_commit_id=new, key="2.10", at="2023-06-02")
    _tag(conn, dep, "v3.0", commit_id=over, main_commit_id=over, key="3", at="2023-07-02")
    row = _bump(conn, repo, dep, version="<3.0", at="2024-01-01")

    depbump.resolve_bumps(conn)
    # 2.10 is newer than 2.9 as a version, though lower as text.
    assert _resolved(conn, row) == (new, "ceiling")


def test_a_release_published_after_the_bump_is_not_a_candidate(bump_env):
    """Bounded by the bump's own date, so the answer cannot drift as later tags
    arrive."""
    conn, repo, dep = bump_env
    early = _commit(conn, dep, "d1" * 20, "2023-01-01")
    later = _commit(conn, dep, "d2" * 20, "2025-01-01")
    _tag(conn, dep, "v2.1", commit_id=early, main_commit_id=early, key="2.1", at="2023-01-02")
    _tag(conn, dep, "v2.9", commit_id=later, main_commit_id=later, key="2.9", at="2025-01-02")
    row = _bump(conn, repo, dep, version="<3.0", at="2024-01-01")

    depbump.resolve_bumps(conn)
    assert _resolved(conn, row) == (early, "ceiling")


def test_a_prerelease_never_matches_the_release_it_precedes(bump_env):
    """Collapsing `-rc1` onto the final release resolves to the wrong commit
    while looking perfectly successful."""
    conn, repo, dep = bump_env
    final = _commit(conn, dep, "ee" * 20, "2024-01-01")
    _tag(conn, dep, "v1.0.0", commit_id=final, main_commit_id=final, key="1")
    row = _bump(conn, repo, dep, version="1.0.0-rc1", at="2024-02-01")

    depbump.resolve_bumps(conn)
    assert _resolved(conn, row) == (None, None)


def test_a_commit_written_after_the_bump_is_rejected(bump_env):
    """Nothing can depend on a commit that does not exist yet, so a match that
    claims otherwise is proof the match is wrong."""
    conn, repo, dep = bump_env
    future = _commit(conn, dep, "ff" * 20, "2025-01-01")
    _tag(conn, dep, "v1.0.0", commit_id=future, main_commit_id=future, key="1")
    row = _bump(conn, repo, dep, version="1.0.0", at="2024-01-01")

    depbump.resolve_bumps(conn)
    assert _resolved(conn, row) == (None, None)
