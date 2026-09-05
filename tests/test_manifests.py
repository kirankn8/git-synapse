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
    ("pom.xml", ("<project><dependencies><dependency><artifactId>guava</artifactId>"
                "<version>32.1.0</version></dependency></dependencies></project>"), "guava", "tag"),
    ("app.csproj", ('<Project><ItemGroup><PackageReference Include="Serilog" Version="3.1.1"/>'
                   "</ItemGroup></Project>"), "Serilog", "tag"),
    ("Gemfile.lock", ("GIT\n  remote: https://github.com/acme/rack.git\n"
                     f"  revision: {SHA}\n"), "rack", "commit"),
    ("pubspec.yaml", "dependencies:\n  http: 1.2.0\n", "http", "tag"),
    (".gitmodules", ('[submodule "vendor/zlib"]\n\tpath = vendor/zlib\n'
                    "\turl = https://github.com/madler/zlib.git\n\tbranch = v1.3\n"), "zlib", "tag"),
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


# ------------------------------------------------- parsers with no coverage

def _with_parser(monkeypatch, parse):
    """Swap the parser for `package.json`. `Ecosystem` is frozen, so the whole
    record is replaced rather than one of its fields."""
    import dataclasses
    eco = dataclasses.replace(M.ecosystem_for("package.json"), parse=parse)
    monkeypatch.setattr(M, "ecosystem_for", lambda path: eco)

def test_a_gemfile_lock_yields_both_gems_and_git_pins():
    """A Gemfile.lock records ordinary gems by version and git dependencies by
    revision, and dropping either loses half the file."""
    lock = """GIT
  remote: https://github.com/acme/widget.git
  revision: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0
  specs:
    widget (1.2.3)

GEM
  remote: https://rubygems.org/
  specs:
    rails (7.0.4)
    rack (2.2.6)
"""
    pairs = M.parse_gemfile_lock(lock)
    assert ("rails", "7.0.4") in pairs and ("rack", "2.2.6") in pairs
    # A git dependency is listed twice: once by revision, once by version in
    # `specs:`. Both are emitted, and the revision comes first, so the stronger
    # evidence is what survives deduplication.
    assert any(n == "widget" and v.startswith("a1b2c3d4") for n, v in pairs)
    widget = [r for r in M.references("Gemfile.lock", lock) if r.name == "widget"]
    assert widget and widget[0].kind == "commit"


def test_a_manifest_whose_parser_raises_is_skipped_not_fatal(monkeypatch):
    """One unreadable manifest in a monorepo must not end the scan for the
    other two hundred."""
    def _explode(_text):
        raise ValueError("malformed")

    _with_parser(monkeypatch, _explode)
    assert M.references("package.json", "{}") == []


def test_a_path_that_is_not_a_manifest_yields_nothing():
    assert M.references("README.md", "# hello") == []


@pytest.mark.parametrize("name", ["Setup uv", "a" * 201])
def test_a_name_that_cannot_be_a_package_is_dropped(name, monkeypatch):
    """A walker that wanders into a CI step title produces things like
    "Setup uv" -- no package name contains whitespace, and none is 200 long."""
    _with_parser(monkeypatch, lambda _t: [(name, "1.0.0")])
    assert M.references("package.json", "{}") == []


def test_a_block_name_is_not_a_dependency(monkeypatch):
    """`dependencies` is the block holding them, not one of them."""
    _with_parser(monkeypatch, lambda _t: [("dependencies", "1.0.0")])
    assert M.references("package.json", "{}") == []


def test_a_bazel_stanza_pins_a_repository_to_a_ref():
    """Bazel, Dockerfiles and deps.edn share no syntax, but each puts a name
    and a hash in one stanza."""
    text = '''
http_archive(
    name = "com_google_absl",
    sha256 = "0123456789abcdef0123456789abcdef01234567",
)
'''
    got = M.parse_pinned_refs(text)
    assert any(name == "com_google_absl" for name, _ in got)


@pytest.mark.parametrize("raw", ["<", ">=", "^"])
def test_a_comparator_with_no_version_bounds_nothing(raw):
    """`>=` on its own is a truncated constraint, not a floor of zero."""
    assert M.bounds(raw) == (None, None)


def test_a_pom_without_an_artifact_publishes_nothing():
    """`groupId` alone does not name a package, and inventing one from it would
    claim this repository publishes something it does not."""
    assert M.published_names("pom.xml", "<project><groupId>com.acme</groupId></project>") == []


def test_yaml_that_is_not_a_mapping_is_not_a_manifest():
    """A workflow file that parses to a list has no dependency block, and
    treating its entries as packages invents them."""
    assert M.references("pnpm-lock.yaml", "- one\n- two\n") == []


def test_a_nested_dependency_table_is_walked(monkeypatch):
    """Cargo writes `serde = { version = "1.0", features = [...] }`; reading only
    the string form would miss every dependency that carries options."""
    toml = '[dependencies]\nserde = { version = "1.0.188", features = ["derive"] }\n'
    names = {r.name: r.raw for r in M.references("Cargo.toml", toml)}
    assert names.get("serde") == "1.0.188"


def test_a_stanza_without_a_submodule_header_takes_its_name_from_the_url():
    """conanfile.txt and a bare .gitmodules fragment record a url and a ref and
    no name, so neither line means anything without the other."""
    text = "url = https://github.com/acme/widget.git\nrevision = v1.2.3\n"
    assert M.parse_ini_like(text) == [("widget", "v1.2.3")]


def test_a_bare_name_slash_version_line_is_a_pin():
    """conanfile.txt writes requirements as `name/version` and nothing else."""
    assert ("widget", "1.2.3") in M.parse_ini_like("widget/1.2.3\n")


def test_yaml_that_cannot_be_parsed_yields_nothing():
    """A workflow with a tab where a space belongs must not stop the scan."""
    assert M.references("pnpm-lock.yaml", "a:\n\t- broken\n") == []


def test_a_version_with_no_digits_is_not_a_version():
    """`name` as a version is what a walker produces when it wanders into a CI
    step definition -- a parse error that reads as a fact."""
    import dataclasses
    eco = dataclasses.replace(M.ecosystem_for("package.json"),
                              parse=lambda _t: [("lodash", "name")])
    from unittest import mock
    with mock.patch.object(M, "ecosystem_for", lambda p: eco):
        assert M.references("package.json", "{}") == []


def test_yaml_manifests_are_skipped_when_the_parser_is_absent(monkeypatch):
    """PyYAML is optional. Without it a lockfile must yield nothing rather than
    raise on every scan."""
    monkeypatch.setattr(M, "yaml", None)
    assert M.references("pnpm-lock.yaml", "packages:\n  /left-pad/1.0.0: {}\n") == []


def test_a_manifest_declaring_a_dtd_is_refused_rather_than_expanded():
    """These files come from repositories we mirror, which is to say from
    anyone. ElementTree expands internal entities, so twenty lines of `pom.xml`
    can define nested entities that expand to gigabytes and take the ingest
    down with it. No real Maven or MSBuild manifest declares a DTD."""
    bomb = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE lolz [<!ENTITY lol "lol">\n'
        ' <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        ' <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>\n'
        "<project><artifactId>&lol3;</artifactId></project>"
    )
    assert M.parse_xml(bomb) == []
    assert M._maven_coordinates(bomb) == []

    # And an ordinary manifest is still read.
    real = (
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencies>'
        "<dependency><groupId>com.google.guava</groupId>"
        "<artifactId>guava</artifactId><version>32.0</version></dependency>"
        "</dependencies></project>"
    )
    assert M.parse_xml(real) == [("guava", "32.0")]
