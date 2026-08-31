"""Manifest parsing, across every ecosystem this reads.

This layer produces the `declared` evidence tier, which agents are told to trust
above everything else, so a parse error here is the most expensive kind: it does
not look like a failure, it looks like a fact.

The property that matters throughout is that a reference is classified at the
right *strength*. Calling a range a commit would present a guess as ground truth.
"""

from __future__ import annotations

import pytest

from git_synapse.analysis import manifests as M

SHA = "5fc63d6f3055aa11b3e0d2ff6a4a06f2c0e0a1b7"


# ------------------------------------------------------------- classification

@pytest.mark.parametrize(("raw", "kind"), [
    ("v3.0.0-20260626221153-5fc63d6f3055", "commit"),   # go pseudo-version
    ("v1.2.3-0.20260626221153-5fc63d6f3055", "commit"), # the '.' separator form
    (SHA, "commit"),
    ("5fc63d6f3055", "commit"),
    ("v1.2.3", "tag"),
    ("1.2.3", "tag"),
    ("2.0.0-rc.1", "tag"),
    ("^1.2.0", "range"),
    ("~=2.1", "range"),
    (">=3,<4", "range"),
    ("*", "range"),
    ("", "range"),
])
def test_a_reference_is_classified_at_its_true_strength(raw, kind):
    assert M.classify("dep", raw, "go").kind == kind


def test_a_pseudo_version_yields_the_upstream_commit():
    ref = M.classify("dep", "v3.0.0-20260626221153-5fc63d6f3055", "go")
    assert ref.sha == "5fc63d6f3055" and ref.provable


def test_a_plain_version_is_never_treated_as_provable():
    """`1.2.3` is hex-ish in places; it must not be read as a commit."""
    ref = M.classify("dep", "1.2.3", "npm")
    assert ref.sha is None and not ref.provable


def test_a_range_pins_nothing():
    assert not M.classify("dep", "^1.2.0", "npm").provable


# ------------------------------------------------------------------ per format

CASES = [
    ("go.mod", "require github.com/acme/signer v1.4.0\n", "github.com/acme/signer", "tag"),
    ("go.mod", "\tgithub.com/acme/signer v3.0.0-20260626221153-5fc63d6f3055\n",
     "github.com/acme/signer", "commit"),
    ("package.json", '{"dependencies": {"@acme/ui": "^1.4.0"}}', "@acme/ui", "range"),
    ("package.json", '{"dependencies": {"@acme/ui": "1.4.0"}}', "@acme/ui", "tag"),
    ("Cargo.toml", '[dependencies]\nserde = "1.0.100"\n', "serde", "tag"),
    ("Cargo.lock", f'[[package]]\nname = "serde"\nversion = "1.0.1"\nsource = "git+x#{SHA}"\n',
     "serde", "tag"),
    ("pyproject.toml", '[project]\ndependencies = []\n[tool.poetry.dependencies]\nrequests = "2.31.0"\n',
     "requests", "tag"),
    ("requirements.txt", "requests==2.31.0\n", "requests", "tag"),
    ("requirements.txt", f"pkg @ git+https://github.com/acme/pkg@{SHA}\n", "pkg", "commit"),
    ("composer.lock", f'{{"packages":[{{"name":"acme/lib","source":{{"reference":"{SHA}"}}}}]}}',
     "acme/lib", "commit"),
    ("flake.lock", f'{{"nodes":{{"nixpkgs":{{"locked":{{"rev":"{SHA}"}}}}}}}}',
     "nixpkgs", "commit"),
    ("Package.resolved", f'{{"pins":[{{"identity":"swift-log","state":{{"revision":"{SHA}"}}}}]}}',
     "swift-log", "commit"),
    ("pom.xml", "<project><dependencies><dependency><artifactId>guava</artifactId>"
                "<version>32.1.0</version></dependency></dependencies></project>", "guava", "tag"),
    ("app.csproj", '<Project><ItemGroup><PackageReference Include="Serilog" Version="3.1.1"/>'
                   "</ItemGroup></Project>", "Serilog", "tag"),
    ("Gemfile.lock", "GIT\n  remote: https://github.com/acme/rack.git\n"
                     f"  revision: {SHA}\n", "rack", "commit"),
    ("pubspec.yaml", "dependencies:\n  http: 1.2.0\n", "http", "tag"),
    (".gitmodules", '[submodule "vendor/zlib"]\n\tpath = vendor/zlib\n'
                    "\turl = https://github.com/madler/zlib.git\n\tbranch = v1.3\n", "zlib", "tag"),
    ("Dockerfile", f"FROM ghcr.io/acme/base@sha256:{SHA}\n", "base", "commit"),
]


@pytest.mark.parametrize(("path", "text", "name", "kind"), CASES)
def test_each_ecosystem_yields_the_expected_reference(path, text, name, kind):
    refs = {r.name: r for r in M.references(path, text)}
    assert name in refs, f"{path}: expected {name}, got {sorted(refs)}"
    assert refs[name].kind == kind


def test_a_github_workflow_pin_is_read_as_a_commit():
    """Pinned actions are a large, and otherwise ignored, source of exact edges."""
    text = "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@" + SHA + "\n"
    refs = {r.name: r for r in M.references(".github/workflows/ci.yml", text)}
    assert refs["actions/checkout"].kind == "commit"
    assert refs["actions/checkout"].sha == SHA


# ------------------------------------------------------------------- robustness

@pytest.mark.parametrize("path", ["Cargo.toml", "package.json", "pom.xml", "flake.lock",
                                  "pubspec.yaml", "go.mod", "app.csproj"])
def test_a_malformed_manifest_yields_nothing_rather_than_raising(path):
    """Repositories nobody controls contain broken files; a scan must survive."""
    for junk in ("", "\x00\x01\x02", "{{{not valid", "<<<<<<< HEAD", "\n" * 100):
        assert M.references(path, junk) == []


def test_an_unknown_filename_is_not_a_manifest():
    assert M.references("README.md", "requests==2.31.0") == []
    assert M.ecosystem_for("main.py") is None


def test_a_commented_out_requirement_is_not_a_dependency():
    assert M.references("requirements.txt", "# requests==2.31.0\n") == []


def test_a_go_module_line_is_not_a_dependency():
    refs = M.references("go.mod", "module github.com/acme/signer\n\ngo 1.22\n")
    assert all(r.name != "github.com/acme/signer" for r in refs)


def test_duplicate_references_collapse():
    text = '{"dependencies": {"a": "1.0.0"}, "devDependencies": {"a": "1.0.0"}}'
    assert len(M.references("package.json", text)) == 1


def test_a_dependency_block_name_is_never_itself_a_dependency():
    refs = M.references("Cargo.toml", '[dependencies]\nserde = "1.0"\n')
    assert all(r.name != "dependencies" for r in refs)


# ------------------------------------------------------------------- coverage

def test_the_popular_ecosystems_are_all_covered():
    """A language with no manifest reader contributes no provable edges at all."""
    expected = {"go", "npm", "python", "rust", "ruby", "php", "java", "dotnet",
                "cpp", "swift", "dart", "elixir", "haskell", "clojure", "julia",
                "r", "perl", "nix", "bazel", "terraform", "docker", "actions"}
    assert expected <= {e.name for e in M.ECOSYSTEMS}


def test_a_lockfile_is_registered_before_the_manifest_beside_it():
    """The lock pins; the loose manifest only constrains."""
    order = [f for e in M.ECOSYSTEMS for f in e.files]
    assert order.index("package-lock.json") < order.index("package.json")
    assert order.index("composer.lock") < order.index("composer.json")
    assert order.index("Gemfile.lock") < order.index("Gemfile")


# ------------------------------------------------- matching a version to a tag

@pytest.mark.parametrize(("declared", "tag"), [
    ("33.4.0-jre",     "v33.4.0"),      # Maven classifier vs git tag
    ("33.4.0-android", "v33.4.0"),      # the other build of the same release
    ("33.4.0",         "guava-33.4.0"), # monorepo component prefix
    ("7.0.0",          "@babel/core@7.0.0"),
    ("1.2.0",          "sub/v1.2.0"),   # Go submodule tag
    ("1.2",            "v1.2.0"),       # trailing zeros are not a difference
    ("1.12.1",         "release-1.12.1"),
    ("2.0a1",          "v2.0.0a1"),     # PEP 440, no separator
    ("1.0.0-rc1",      "v1.0.0-rc1"),   # a prerelease matches its own tag
    ("0.10",           "release_0_10"), # older Java: underscores, not dots
    ("1.2.3",          "VERSION_1_2_3"),
    ("1.2.3",          "R_1_2_3"),      # autotools
])
def test_a_declared_version_matches_the_tag_that_shipped_it(declared, tag):
    """A manifest names versions in the registry's namespace and git names them
    in the repository's, so the two are never equal as strings."""
    assert M.version_key(declared) == M.version_key(tag)


@pytest.mark.parametrize(("a", "b"), [
    ("1.0.0-rc1", "1.0.0"),    # a release candidate is a different commit
    ("1.0.0-beta", "1.0.0"),
    ("33.5.0-SNAPSHOT", "33.5.0"),   # an unreleased build was never tagged
    ("1.10", "1.1"),
    ("2.0", "2.0.1"),
])
def test_versions_that_are_not_the_same_release_never_collapse(a, b):
    """Stripping every suffix would resolve a release candidate to the final
    release while looking perfectly successful."""
    assert M.version_key(a) != M.version_key(b)


@pytest.mark.parametrize("raw", ["*", "latest", "", "   ", "not-a-version"])
def test_a_string_with_no_version_has_no_key(raw):
    assert M.version_key(raw) is None


@pytest.mark.parametrize(("raw", "floor", "ceiling"), [
    ("^4.17.21", "4.17.21", None),
    ("~1.2.3",   "1.2.3",   None),
    ("~> 7.0",   "7.0",     None),
    (">=2,<3",   "2",       "3"),
    ("<3.0",     None,      "3.0"),
    (">1.0",     "1.0",     None),
    ("4.17.21",  "4.17.21", None),
    ("*",        None,      None),
    ("5.5.*",    "5.5",     None),   # Composer and npm wildcards state a floor
    ("4.1.*",    "4.1",     None),
    ("1.0.x",    "1.0",     None),
    # A suffix merely ending in the letter is not a wildcard.
    ("1.0.0-linux", "1.0.0-linux", None),
])
def test_a_range_declares_its_own_bounds(raw, floor, ceiling):
    """The floor is parsed, never guessed: `^4.17.21` states 4.17.21 itself."""
    assert M.bounds(raw) == (floor, ceiling)
