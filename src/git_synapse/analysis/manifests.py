"""Dependency references, read from the manifests of many ecosystems.

Every package manager solves the same problem the same way: a consumer records,
in a file it commits, which version of a dependency it is built against. That
record is **dated** (it lives in a commit), **directional** (the consumer names
the dependency, never the reverse) and **provable** (it is a literal string, not
an inference). It is the only evidence in this system that needs no statistical
argument, which is why it is worth reading properly for every ecosystem rather
than well for one.

What a reference resolves to
----------------------------
References come in three strengths, and the distinction is kept because it is
exactly the confidence of the resulting edge:

``commit``
    The manifest names a commit outright. A Go pseudo-version embeds one, a git
    submodule *is* one, and most lockfiles record one because pinning exactly is
    their whole purpose: ``composer.lock`` has ``reference``, ``Package.resolved``
    has ``revision``, ``flake.lock`` has ``rev``, ``Cargo.lock``, ``mix.lock``
    and ``Gemfile.lock`` carry git revisions. Nothing needs resolving.

``tag``
    The manifest names a release -- ``v1.2.3``. Resolvable to a commit when the
    upstream repository's tags are known, which is what extends this to the
    ecosystems that never record SHAs at all: Maven, NuGet, Gradle, plain npm.

``range``
    ``^1.2.0``, ``~=2.1``, ``>=3,<4``. A constraint, not a version. Recorded so
    the dependency edge exists, but it pins no commit and is never presented as
    though it did.

Parsing
-------
Real parsers, not regex, wherever the format has one: ``tomllib`` and
``xml.etree`` from the standard library and PyYAML for the rest. A regex reading
of TOML mishandles nested tables, arrays of tables and multi-line strings, and a
manifest that parses *almost* correctly is worse than one that fails loudly.
Formats with no parser -- go.mod, requirements.txt, Gemfile.lock, deps.edn --
keep bespoke readers, because there is nothing else to use.

Deliberately not resolved by network lookup: asking a registry what ``1.2.3``
means would make the answer depend on a third party that can change it, and the
point of this table is that every row can be re-derived from the repositories
themselves.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable

try:                                     # PyYAML is required, but a missing
    import yaml                          # optional extra must not break ingest.
except ImportError:                      # pragma: no cover - packaging guard
    yaml = None

log = logging.getLogger(__name__)

#: A 40- or 12-character hex string: the two forms a manifest ever writes.
_SHA = re.compile(r"\b([0-9a-f]{40}|[0-9a-f]{12})\b")

#: Go pseudo-version. The separator before the timestamp is '-' in
#: vX.0.0-<ts>-<sha> but '.' in vX.Y.Z-0.<ts>-<sha>; accepting only '-'
#: silently drops a sixth of them.
_PSEUDO = re.compile(r"[-.](\d{14})-([0-9a-f]{12})$")

#: A release tag as manifests write it, with or without the leading v.
_TAG = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?$")

#: Keys under which a parsed manifest records an exact commit.
_PIN_KEYS = ("rev", "revision", "reference", "resolved-ref", "commit", "sha", "digest")

#: Keys under which it records a version or tag.
_VERSION_KEYS = ("version", "tag", "ref")

#: Wrapper objects that hold a pin but are not themselves the package: the name
#: lives on the parent, so recursing through these must not rename the entry.
_STRUCTURAL = ("locked", "source", "state", "dist", "original", "metadata", "resolved")

#: Mapping blocks that hold `name -> constraint` pairs.
_DEP_BLOCKS = ("dependencies", "devdependencies", "dev-dependencies", "peerdependencies",
               "optionaldependencies", "build-dependencies", "resolutions", "requires",
               "packages", "imports", "deps", "require", "require-dev")


@dataclass(frozen=True)
class Reference:
    """One dependency reference found in one manifest."""

    name: str
    raw: str
    kind: str          # commit | tag | range
    ecosystem: str
    sha: str | None = None

    @property
    def provable(self) -> bool:
        """True when this pins an exact commit with no further resolution."""
        return self.kind == "commit" and bool(self.sha)


#: Suffixes that name a *build* of a release rather than a different release.
#: Guava ships `33.4.0-jre` and `33.4.0-android` from one tag, `v33.4.0`.
#: Strictly an allowlist: `-rc1` and `-beta` are separate releases with their
#: own tags and their own commits, and collapsing them would resolve a release
#: candidate to the final release while looking perfectly successful.
_CLASSIFIERS = frozenset(("jre", "android", "ga", "final"))

#: The version at the end of a string, ignoring whatever precedes it. Absorbs
#: every component prefix a monorepo invents -- `guava-33.4.0`, `sub/v1.2.0`,
#: `@babel/core@7.0.0` -- without needing to know the package's name.
#: The separator before a suffix is optional because PEP 440 writes `2.0a1`
#: with none; requiring one made that version canonicalise to its own trailing
#: digit. Underscores separate numbers as well as dots, because the older Java
#: and autotools convention tags `release_0_10` and `VERSION_1_2_3` -- without
#: that, Truth's every tag reduced to its last number alone.
_VERSION_AT_END = re.compile(
    r"(\d+(?:[._]\d+)*)([-+._]?[A-Za-z][0-9A-Za-z.+-]*)?$")

#: A comparator and the version it bounds, as ranges are written everywhere:
#: `^4.17.21`, `~> 7.0`, `>=2, <3`, `<3.0`.
_BOUND = re.compile(r"(>=|<=|==|>|<|\^|~>|~|=)?\s*(\d[0-9A-Za-z.+-]*)")


def version_key(raw: str) -> str | None:
    """Canonical form of a version or tag name, for matching one to the other.

    A manifest names versions in the package registry's namespace and git names
    them in the repository's, so the two are never equal as strings:
    `33.4.0-jre` against `v33.4.0`. Reducing both to the same key makes the
    match an indexed join rather than a pile of transformations at lookup time.

    Trailing zeros are dropped so `1.2` and `1.2.0` agree, and a prerelease
    suffix is kept so `1.0.0-rc1` never collapses onto `1.0.0`.

    Returns None when there is no version in the string at all.
    """
    text = (raw or "").strip().strip("\"'")
    if not (m := _VERSION_AT_END.search(text)):
        return None
    numbers = [int(part) for part in re.split(r"[._]", m.group(1))]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    key = ".".join(str(n) for n in numbers)
    suffix = (m.group(2) or "").lstrip("-+._").lower()
    return f"{key}-{suffix}" if suffix and suffix not in _CLASSIFIERS else key


def bounds(raw: str) -> tuple[str | None, str | None]:
    """The `(floor, ceiling)` a range declares, as written.

    Neither is inferred: `^4.17.21` states 4.17.21 as its own lower bound, and
    that is the version taken. What actually got installed may have drifted
    above it, but a manifest left untouched is one where nothing had to adapt --
    so the floor is the last version anyone made a decision about.
    """
    text = (raw or "").strip().strip("\"'")
    floor = ceiling = None
    for comparator, version in _BOUND.findall(text):
        if comparator in ("<", "<="):
            ceiling = ceiling or version
        else:                       # ^ ~ ~> >= > == = or a bare version
            floor = floor or version
    return floor, ceiling


def classify(name: str, raw: str, ecosystem: str) -> Reference:
    """Turn a raw version string into a reference of the right strength."""
    value = (raw or "").strip().strip("\"'")
    if not value:
        return Reference(name, raw or "", "range", ecosystem)
    # A sole `==` pins one version; anything compound stays a constraint.
    if (exact := re.fullmatch(r"={2,3}\s*([^,;\s]+)", value)):
        value = exact.group(1)
    if (pseudo := _PSEUDO.search(value)):
        return Reference(name, value, "commit", ecosystem, pseudo.group(2))
    # A bare SHA, but not a version that merely looks hex-ish, such as "1.2.3".
    if (bare := _SHA.search(value)) and not _TAG.match(value):
        return Reference(name, value, "commit", ecosystem, bare.group(1))
    if _TAG.match(value):
        return Reference(name, value, "tag", ecosystem)
    return Reference(name, value, "range", ecosystem)


# --------------------------------------------------------------- loading --

def _load_toml(text: str) -> Any:
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None


def _load_yaml(text: str) -> Any:
    if yaml is None:
        return None
    try:
        return yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - any malformed document is simply skipped
        return None


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _walk(doc: Any) -> list[tuple[str, str]]:
    """Every (name, pin) a parsed manifest carries, whatever its shape.

    One walker for TOML, YAML and JSON: once parsed they are all dicts and lists,
    and every ecosystem ends up with a package object holding a pin. Walking for
    the pin survives format-version changes that modelling each layout does not.
    """
    out: list[tuple[str, str]] = []

    def visit(node: Any, label: str) -> None:
        if isinstance(node, dict):
            explicit = node.get("name") or node.get("package") or node.get("identity")
            name = str(explicit or label or "").strip()
            # A pin beats a version: it is the stronger evidence.
            for key in _PIN_KEYS + _VERSION_KEYS:
                value = node.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    if name:
                        out.append((name, str(value)))
                    break
            for key, value in node.items():
                lowered = str(key).lower()
                if lowered in _DEP_BLOCKS and isinstance(value, dict):
                    for dep, spec in value.items():
                        if isinstance(spec, (str, int, float)):
                            out.append((str(dep), str(spec)))
                        elif isinstance(spec, dict):
                            visit(spec, str(dep))
                else:
                    # Keep the package name across wrapper objects; otherwise the
                    # key is the name, as in `{"nixpkgs": {...}}`.
                    child = name if (explicit or lowered in _STRUCTURAL) else str(key)
                    visit(value, child)
        elif isinstance(node, list):
            for item in node:
                visit(item, label)

    visit(doc, "")
    return out


def parse_toml(text: str) -> list[tuple[str, str]]:
    return _walk(_load_toml(text))


def parse_yaml(text: str) -> list[tuple[str, str]]:
    doc = _load_yaml(text)
    out = _walk(doc)
    # CI workflows pin actions as `uses: owner/repo@ref`, which is not a mapping.
    for m in re.finditer(r"uses:\s*([A-Za-z0-9._/-]+)@([0-9a-fA-F]{7,40}|v[\d.]+)", text):
        out.append((m.group(1), m.group(2)))
    return out


def parse_json_pins(text: str) -> list[tuple[str, str]]:
    return _walk(_load_json(text))


def parse_json_deps(text: str) -> list[tuple[str, str]]:
    """package.json-style: name -> version across every dependency block."""
    doc = _load_json(text)
    if not isinstance(doc, dict):
        return []
    out = []
    for block, section in doc.items():
        if str(block).lower() in _DEP_BLOCKS and isinstance(section, dict):
            out += [(k, str(v)) for k, v in section.items() if isinstance(v, (str, int, float))]
    return out or _walk(doc)


def parse_xml(text: str) -> list[tuple[str, str]]:
    """Maven and MSBuild, which are XML and should be read as XML."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    strip = lambda t: t.rsplit("}", 1)[-1]  # noqa: E731 - drop the XML namespace
    for el in root.iter():
        tag = strip(el.tag)
        if tag == "dependency":
            kids = {strip(c.tag): (c.text or "").strip() for c in el}
            name = kids.get("artifactId") or kids.get("groupId")
            if name and kids.get("version"):
                out.append((name, kids["version"]))
        elif tag in ("PackageReference", "PackageVersion"):
            name = el.get("Include") or el.get("Update")
            version = el.get("Version") or el.get("version")
            if name and version:
                out.append((name, version))
    return out


def parse_go(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("//", "exclude ", "module ")):
            continue
        line = line.split("//", 1)[0]
        if (m := re.search(r"([A-Za-z0-9._~-]+(?:\.[A-Za-z]{2,})/[^\s]+)\s+(v\S+)", line)):
            out.append((m.group(1), m.group(2)))
    return out


def parse_requirements(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if (m := re.search(r"git\+[^@\s]+/([A-Za-z0-9._-]+?)(?:\.git)?@([0-9a-fA-F]{7,40})", line)):
            out.append((m.group(1), m.group(2)))
        elif (m := re.match(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*([=<>!~^].+)$", line)):
            out.append((m.group(1), m.group(2).strip()))
    return out


def parse_gemfile_lock(text: str) -> list[tuple[str, str]]:
    out, current = [], ""
    for line in text.splitlines():
        if (m := re.match(r"\s+remote:\s*\S+/([A-Za-z0-9._-]+?)(?:\.git)?/?$", line)):
            current = m.group(1)
        elif (m := re.match(r"\s+revision:\s*([0-9a-f]{7,40})", line)) and current:
            out.append((current, m.group(1)))
        elif (m := re.match(r"^\s{4}([A-Za-z0-9._-]+)\s+\(([^)]+)\)", line)):
            out.append((m.group(1), m.group(2)))
    return out


def parse_pinned_refs(text: str) -> list[tuple[str, str]]:
    """Formats with no parser that still pin a repository to a ref.

    Bazel's `http_archive`/`git_repository`, Dockerfiles pinned by digest, Zig's
    build.zig.zon and Clojure's deps.edn share no syntax, but all put a name and
    a hash in the same stanza.
    """
    out = []
    for m in re.finditer(r'(?:name|repo|url|image|:git/url)\s*[=:]\s*"?([^\s",]+)"?'
                         r'(?:[^\n]*\n){0,6}?[^\n]*?'
                         r'(?:commit|sha|:git/sha|tag|digest|hash|sha256)\s*[=:]\s*"?'
                         r'([0-9a-fA-F]{7,64}|v[\d][\w.\-]*)"?', text):
        name = m.group(1).rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if name:
            out.append((name, m.group(2)))
    for m in re.finditer(r"FROM\s+(\S+?)(?::[^@\s]+)?@sha256:([0-9a-f]{12,64})", text):
        out.append((m.group(1).rsplit("/", 1)[-1], m.group(2)))
    return out


def parse_ini_like(text: str) -> list[tuple[str, str]]:
    """.gitmodules, conanfile.txt and other key=value stanzas."""
    out, current = [], ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if (m := re.match(r'^\[submodule\s+"([^"]+)"\]', line)):
            current = m.group(1).rstrip("/").rsplit("/", 1)[-1]
        elif (m := re.match(r"^(?:url|path)\s*=\s*(\S+)", line)) and not current:
            current = m.group(1).rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        elif (m := re.match(r"^(?:branch|tag|revision)\s*=\s*(\S+)", line)) and current:
            out.append((current, m.group(1)))
        elif (m := re.match(r"^([A-Za-z0-9._-]+)/([\w.\-]+)$", line)):
            out.append((m.group(1), m.group(2)))
    return out


@dataclass(frozen=True)
class Ecosystem:
    """One package manager: which files to read, and how."""

    name: str
    files: tuple[str, ...]
    parse: Callable[[str], list[tuple[str, str]]]


#: Ordered so a lockfile wins over the loose manifest beside it: the lock pins,
#: the manifest only constrains.
ECOSYSTEMS: tuple[Ecosystem, ...] = (
    Ecosystem("go", ("go.mod",), parse_go),

    Ecosystem("npm", ("package-lock.json", "npm-shrinkwrap.json"), parse_json_pins),
    Ecosystem("npm", ("package.json",), parse_json_deps),
    Ecosystem("npm", ("pnpm-lock.yaml",), parse_yaml),
    Ecosystem("npm", ("yarn.lock", "bun.lock"), parse_pinned_refs),
    Ecosystem("deno", ("deno.json", "deno.jsonc", "import_map.json"), parse_json_deps),

    Ecosystem("python", ("Pipfile.lock", "uv.lock"), parse_json_pins),
    Ecosystem("python", ("poetry.lock", "pyproject.toml", "Pipfile"), parse_toml),
    Ecosystem("python", ("requirements.txt", "requirements-dev.txt", "constraints.txt",
                         "dev-requirements.txt", "test-requirements.txt"), parse_requirements),
    Ecosystem("python", ("environment.yml", "conda.yaml"), parse_yaml),

    Ecosystem("rust", ("Cargo.lock", "Cargo.toml"), parse_toml),

    Ecosystem("ruby", ("Gemfile.lock",), parse_gemfile_lock),
    Ecosystem("ruby", ("Gemfile", "gems.rb"), parse_pinned_refs),

    Ecosystem("php", ("composer.lock",), parse_json_pins),
    Ecosystem("php", ("composer.json",), parse_json_deps),

    Ecosystem("java", ("pom.xml",), parse_xml),
    Ecosystem("java", ("libs.versions.toml",), parse_toml),
    Ecosystem("java", ("build.gradle", "build.gradle.kts", "build.sbt",
                       "gradle.properties"), parse_pinned_refs),

    Ecosystem("dotnet", ("Directory.Packages.props", "packages.config"), parse_xml),
    Ecosystem("dotnet", ("paket.lock",), parse_pinned_refs),

    Ecosystem("cpp", (".gitmodules", "conanfile.txt", "conan.lock"), parse_ini_like),
    Ecosystem("cpp", ("vcpkg.json",), parse_json_deps),
    Ecosystem("cpp", ("CMakeLists.txt", "conanfile.py", "meson.build"), parse_pinned_refs),

    Ecosystem("swift", ("Package.resolved", "Cartfile.resolved"), parse_json_pins),
    Ecosystem("swift", ("Package.swift", "Podfile.lock", "Podfile"), parse_pinned_refs),

    Ecosystem("dart", ("pubspec.lock", "pubspec.yaml"), parse_yaml),

    Ecosystem("elixir", ("mix.lock", "mix.exs"), parse_pinned_refs),
    Ecosystem("erlang", ("rebar.lock", "rebar.config"), parse_pinned_refs),

    Ecosystem("haskell", ("stack.yaml", "stack.yaml.lock"), parse_yaml),
    Ecosystem("haskell", ("cabal.project.freeze",), parse_pinned_refs),

    Ecosystem("clojure", ("deps.edn", "project.clj"), parse_pinned_refs),
    Ecosystem("julia", ("Project.toml", "Manifest.toml"), parse_toml),
    Ecosystem("r", ("renv.lock",), parse_json_pins),
    Ecosystem("r", ("DESCRIPTION",), parse_ini_like),
    Ecosystem("perl", ("cpanfile", "cpanfile.snapshot"), parse_pinned_refs),
    Ecosystem("lua", ("rockspec",), parse_pinned_refs),
    Ecosystem("ocaml", ("dune-project", "opam"), parse_pinned_refs),
    Ecosystem("zig", ("build.zig.zon",), parse_pinned_refs),
    Ecosystem("nim", ("nimble.lock",), parse_json_pins),

    Ecosystem("nix", ("flake.lock",), parse_json_pins),
    Ecosystem("bazel", ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel"), parse_pinned_refs),
    Ecosystem("terraform", (".terraform.lock.hcl", "main.tf", "versions.tf"), parse_pinned_refs),
    Ecosystem("helm", ("Chart.lock", "Chart.yaml"), parse_yaml),
    Ecosystem("docker", ("Dockerfile", "docker-compose.yml"), parse_pinned_refs),
    Ecosystem("actions", ("action.yml", "action.yaml"), parse_yaml),
)

#: Matched by suffix rather than by exact name.
SUFFIX_MATCH: tuple[tuple[str, str, Callable], ...] = (
    (".csproj", "dotnet", parse_xml), (".fsproj", "dotnet", parse_xml),
    (".vbproj", "dotnet", parse_xml), (".nuspec", "dotnet", parse_xml),
    (".cabal", "haskell", parse_pinned_refs), (".rockspec", "lua", parse_pinned_refs),
    (".opam", "ocaml", parse_pinned_refs), (".nimble", "nim", parse_pinned_refs),
    (".podspec", "swift", parse_pinned_refs), (".gemspec", "ruby", parse_pinned_refs),
)

#: Every basename worth reading, for the tree scan.
MANIFEST_FILES: tuple[str, ...] = tuple(dict.fromkeys(f for e in ECOSYSTEMS for f in e.files))

#: Workflow files live under a fixed directory rather than having a fixed name.
WORKFLOW_DIR = ".github/workflows/"


def ecosystem_for(path: str) -> Ecosystem | None:
    """The parser for a manifest path, or None if it is not a manifest."""
    basename = path.rsplit("/", 1)[-1]
    for eco in ECOSYSTEMS:
        if basename in eco.files:
            return eco
    for suffix, name, parser in SUFFIX_MATCH:
        if basename.endswith(suffix):
            return Ecosystem(name, (basename,), parser)
    if WORKFLOW_DIR in path and basename.endswith((".yml", ".yaml")):
        return Ecosystem("actions", (basename,), parse_yaml)
    return None


def references(path: str, text: str) -> list[Reference]:
    """Every dependency reference in one manifest, classified by strength."""
    eco = ecosystem_for(path)
    if eco is None:
        return []
    try:
        pairs = eco.parse(text)
    except Exception as exc:  # noqa: BLE001 - one bad manifest must not stop a scan
        log.warning("could not parse %s: %s", path, exc)
        return []

    seen, out = set(), []
    for name, raw in pairs:
        name = (name or "").strip()
        if not name or len(name) > 200 or name.lower() in _DEP_BLOCKS:
            continue
        # No package name contains whitespace. This is what a CI step title
        # ("Setup uv") looks like when a walker mistakes it for a package.
        if any(c.isspace() for c in name):
            continue
        # A version says something about a number. Prose does not: django ships
        # `docs/.../constraints.txt`, which is documentation, and parsing it as a
        # pip constraints file invented dependencies called `expressions` and
        # `name` -- a parse error that reads as a fact.
        if not any(c.isdigit() for c in str(raw)):
            continue
        ref = classify(name, raw, eco.name)
        if (ref.name, ref.raw) not in seen:
            seen.add((ref.name, ref.raw))
            out.append(ref)
    return out
