"""Manifest parsing and dependency extraction.

This layer produces the `declared` evidence tier, which agents are told to trust
above everything else, so a parse error here is the most expensive kind: it does
not look like a failure, it looks like a fact.
"""
from __future__ import annotations

import subprocess

import pytest

from git_synapse.analysis.depbump import (
    MANIFESTS,
    _parse_manifest_line,
    _PSEUDO,
    declared_at_head,
    declared_modules_at_head,
    extract_from_mirror,
    manifest_paths,
    patterns_for,
)

#: Patterns compiled for one internal owner. Passed explicitly so these tests
#: describe the parser rather than whatever happens to be in the database.
PATS = patterns_for(("acme",))


# ------------------------------------------------------------------ go.mod

@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Happy: a plain require, with and without a major-version suffix.
        ("\tgithub.com/acme/contracts v1.2.3", ("contracts", "v1.2.3")),
        ("\tgithub.com/acme/contracts/v2 v2.0.1", ("contracts", "v2.0.1")),
        # Indirect is still a declaration in the file.
        ("\tgithub.com/acme/gomi v0.1.0 // indirect", ("gomi", "v0.1.0")),
        # A replace directive names a real relationship.
        ("replace github.com/acme/telemetry => ../../telemetry", ("telemetry", "=>")),
        # Pseudo-version, the shape bump tracking depends on.
        (
            "\tgithub.com/acme/signer v3.0.0-20260626221153-5fc63d6f3055",
            ("signer", "v3.0.0-20260626221153-5fc63d6f3055"),
        ),
        # Negative: commented out, in three styles.
        ("// replace github.com/acme/hit => ../hit", None),
        ("//replace github.com/acme/hit => ../hit", None),
        ("  //  github.com/acme/hit v1.0.0", None),
        # Negative: exclude is the opposite of a requirement.
        ("exclude github.com/acme/cluster-api-provider-libvirt v0.1.2", None),
        # Negative: another org, and a lookalike host.
        ("\tgithub.com/acme-public/thing v1.0.0", None),
        ("\tgitlab.com/acme/contracts v1.0.0", None),
        # Corner: empty, whitespace, and a bare module line.
        ("", None),
        ("   ", None),
        ("module github.com/acme/runtime", None),
    ],
)
def test_go_manifest_lines(line, expected):
    assert _parse_manifest_line(line, "go", PATS) == expected


def test_trailing_comment_does_not_hide_a_real_requirement():
    """Only the code before `//` declares anything, but it still counts."""
    got = _parse_manifest_line(
        "\tgithub.com/acme/contracts v1.2.3 // pinned, see ACME-1", "go", PATS
    )
    assert got == ("contracts", "v1.2.3")


def test_a_module_path_inside_a_trailing_comment_is_not_a_dependency():
    got = _parse_manifest_line(
        "\tgithub.com/other/thing v1.0.0 // github.com/acme/contracts v9", "go", PATS
    )
    assert got is None


# -------------------------------------------------------------------- npm

@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('    "@acme/ui-apis": "^1.4.0",', ("ui-apis", "^1.4.0")),
        ('"@acme/design": "0.0.1"', ("design", "0.0.1")),
        ('    "react": "^18.0.0",', None),
        ('    "@other/design": "1.0.0",', None),
        ("", None),
    ],
)
def test_npm_manifest_lines(line, expected):
    assert _parse_manifest_line(line, "npm", PATS) == expected


def test_unknown_ecosystem_parses_nothing():
    assert _parse_manifest_line("github.com/acme/contracts v1", "rust", PATS) is None


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
    got = dict(declared_at_head(mirror, "runtime", PATS, "go.mod", "go"))
    assert got == {"contracts": "v1.2.3"}


def test_declared_at_head_on_a_missing_manifest_is_empty_not_an_error(tmp_path):
    mirror = _repo(tmp_path, {"README.md": "x\n"})
    assert declared_at_head(mirror, "any", PATS, "go.mod", "go") == []


def test_every_declared_manifest_kind_has_a_parser():
    """MANIFESTS drives the scan; a kind with no parser scans to nothing."""
    probes = {
        "go": ("\tgithub.com/acme/contracts v1.0.0", ("contracts", "v1.0.0")),
        "npm": ('"@acme/contracts": "1.0.0"', ("contracts", "1.0.0")),
    }
    for _, ecosystem in MANIFESTS:
        assert ecosystem in probes, f"{ecosystem} is scanned but has no parser test"
        line, expected = probes[ecosystem]
        assert _parse_manifest_line(line, ecosystem, PATS) == expected


# -------------------------------------------------- the intra-repo module graph

def _module_edges(mirror, repo_name):
    """Every internal edge across every manifest, the way refresh_modules does."""
    from git_synapse.analysis.depbump import declared_modules_at_head

    edges = set()
    for manifest, _ in manifest_paths(mirror):
        for consumer, dep, _version in declared_modules_at_head(mirror, repo_name, manifest, PATS):
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

    bumps = list(extract_from_mirror(bare, "consumer", PATS, "go.mod", "go"))
    assert len(bumps) >= 4, f"four bumps in history, found {len(bumps)}"
    assert all(b.dep_name == "upstream" for b in bumps)
    # Each pseudo-version carries the upstream commit it pinned.
    assert all(b.dep_sha for b in bumps)


# ------------------------------------------------- the parser's darker corners

@pytest.mark.parametrize(("line", "expected"), [
    ('"console-sdk": "github:acme/console-sdk-js#v1.2.0"',
     ("console-sdk-js", "v1.2.0")),
    ('"sdk": "git+https://github.com/acme/console-sdk-js.git#main"',
     ("console-sdk-js", "main")),
    # No fragment: the dependency is real but unpinned, which is worth an edge
    # with a version we can distinguish from a tag.
    ('"sdk": "github:acme/telemetry"', ("telemetry", "git")),
    # Someone else's fork of the same name is not an internal dependency.
    ('"sdk": "github:someoneelse/telemetry#v1"', None),
])
def test_an_npm_package_pinned_to_a_git_url_is_still_an_internal_dependency(line, expected):
    assert _parse_manifest_line(line, "npm", PATS) == expected


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
    assert extract_from_mirror(empty, "anything", PATS) == []


def test_a_diff_line_before_any_commit_marker_is_discarded(tmp_path, monkeypatch):
    """Attributing a bump to the wrong commit is worse than dropping it: the
    whole value of a bump edge is the date and SHA it carries."""
    import subprocess as sp

    orphan = "+\tgithub.com/acme/httpkit v0.0.0-20260101000000-abcdef123456\n"

    class _Proc:
        returncode = 0
        stdout = orphan + "@@" + "a" * 40 + "\n" + orphan
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda *a, **k: _Proc())
    edges = extract_from_mirror(tmp_path, "consumer", PATS)
    assert len(edges) == 1
    assert edges[0].consumer_sha == "a" * 40


def test_the_repositorys_own_module_line_is_not_a_dependency_on_itself(tmp_path, monkeypatch):
    import subprocess as sp

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ("@@" + "b" * 40 + "\n"
                  "+module github.com/acme/httpkit v1.0.0\n"
                  "+\tgithub.com/acme/telemetry v1.2.3\n")

    monkeypatch.setattr(sp, "run", lambda *a, **k: _Proc())
    edges = extract_from_mirror(tmp_path, "httpkit", PATS)
    assert [e.dep_name for e in edges] == ["telemetry"]


def test_declared_at_head_skips_the_repositorys_own_module_line(tmp_path):
    mirror = _repo(tmp_path, {"go.mod": (
        "module github.com/acme/httpkit\n"
        "require (\n"
        "\tgithub.com/acme/httpkit v1.0.0\n"
        "\tgithub.com/acme/telemetry v2.0.0\n"
        ")\n"
    )})
    names = [n for n, _ in declared_at_head(mirror, "httpkit", PATS, "go.mod", "go")]
    assert names == ["telemetry"]


def test_declared_modules_on_a_non_repository_is_empty(tmp_path):
    empty = tmp_path / "notgit"
    empty.mkdir()
    assert declared_modules_at_head(empty, "anything", "go.mod", PATS) == []


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
    edges = declared_modules_at_head(mirror, "mono", "gateway/go.mod", PATS)
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
    edges = declared_modules_at_head(mirror, "mono", "cmd/go.mod", PATS)
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
