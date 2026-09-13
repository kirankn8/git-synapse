"""Dependency references, read from the manifests of many ecosystems."""

from __future__ import annotations

import json
import logging
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

try:                                     # PyYAML is required, but a missing
    import yaml  # optional extra must not break ingest.
except ImportError:                      # pragma: no cover - packaging guard
    yaml = None

log = logging.getLogger(__name__)

#: A 40- or 12-character hex string: the two forms a manifest ever writes.
_SHA = re.compile(r"\b([0-9a-f]{40}|[0-9a-f]{12})\b")

# Go pseudo-version: '-' before the timestamp in vX.0.0-<ts>-<sha>, '.' in vX.Y.Z-0.<ts>-<sha>.
_PSEUDO = re.compile(r"[-.](\d{14})-([0-9a-f]{12})$")

#: A release tag as manifests write it, with or without the leading v.
_TAG = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?$")

#: Keys under which a parsed manifest records an exact commit.
_PIN_KEYS = ("rev", "revision", "reference", "resolved-ref", "commit", "sha", "digest")

#: Keys under which it records a version or tag.
_VERSION_KEYS = ("version", "tag", "ref")

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


_CLASSIFIERS = frozenset(("jre", "android", "ga", "final"))

_VERSION_AT_END = re.compile(
    r"(\d+(?:[._]\d+)*)([-+._]?[A-Za-z][0-9A-Za-z.+-]*)?$")

_BOUND = re.compile(r"(>=|<=|==|>|<|\^|~>|~|=)?\s*(\d[0-9A-Za-z.+-]*)")

# 5.5.* and 1.0.x; requiring the separator leaves 1.0.0-linux alone.
_WILDCARD_TAIL = re.compile(r"[.\-][*xX]$")


def version_key(raw: str) -> str | None:
    """Canonical form of a version or tag name, for matching one to the other."""
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
    """The `(floor, ceiling)` a range declares, as written."""
    text = (raw or "").strip().strip("\"'")
    floor = ceiling = None
    for comparator, raw_version in _BOUND.findall(text):
        version = _WILDCARD_TAIL.sub("", raw_version).rstrip(".")
        if comparator in ("<", "<="):
            ceiling = ceiling or version
        else:                       # ^ ~ ~> >= > == = or a bare version
            floor = floor or version
    return floor, ceiling


def _dig(doc: Any, path: tuple[str, ...]) -> list[str]:
    for key in path:
        if not isinstance(doc, dict):
            return []
        doc = doc.get(key)
    return [doc] if isinstance(doc, str) else []


def published_names(path: str, text: str) -> list[str]:
    """The package coordinates this manifest publishes under."""
    base = path.rsplit("/", 1)[-1]
    if base == "go.mod":
        m = re.search(r"^\s*module\s+(\S+)", text, re.MULTILINE)
        return [m.group(1)] if m else []
    if base == "pom.xml":
        return _maven_coordinates(text)
    if base.endswith(".gemspec"):
        m = re.search(r"""\.name\s*=\s*["']([^"']+)["']""", text)
        return [m.group(1)] if m else []
    if base in {"package.json", "composer.json"}:
        doc = _load_json(text)
        return [doc["name"]] if isinstance(doc, dict) and isinstance(doc.get("name"), str) else []
    if base == "Cargo.toml":
        return _dig(_load_toml(text), ("package", "name"))
    if base == "pyproject.toml":
        doc = _load_toml(text)
        return _dig(doc, ("project", "name")) or _dig(doc, ("tool", "poetry", "name"))
    return []


def _maven_coordinates(text: str) -> list[str]:
    """`groupId:artifactId` for a pom, inheriting the group from its parent."""
    root = _parse_xml(text)
    if root is None:
        return []
    ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

    def find(parent: Any, tag: str) -> str | None:
        node = parent.find(f"m:{tag}", ns) if ns else parent.find(tag)
        return node.text.strip() if node is not None and node.text else None

    artifact = find(root, "artifactId")
    if not artifact:
        return []
    parent = root.find("m:parent", ns) if ns else root.find("parent")
    group = find(root, "groupId") or (find(parent, "groupId") if parent is not None else None)
    return [f"{group}:{artifact}"] if group else [artifact]


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
    """Every (name, pin) a parsed manifest carries, whatever its shape."""
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


def parse_conda(text: str) -> list[tuple[str, str]]:
    """Read conda's scalar dependency list as well as its pip subsection."""
    doc = _load_yaml(text)
    if not isinstance(doc, dict):
        return []
    out: list[tuple[str, str]] = []
    for item in doc.get("dependencies", []):
        if isinstance(item, str):
            match = re.match(r"^([A-Za-z0-9_.-]+)\s*(?:=|==)\s*(\S+)$", item.strip())
            if match:
                out.append((match.group(1), match.group(2)))
        elif isinstance(item, dict):
            for pip_item in item.get("pip", []):
                if isinstance(pip_item, str):
                    match = re.match(
                        r"^([A-Za-z0-9_.-]+)\s*(?:==|=)\s*(\S+)$", pip_item.strip()
                    )
                    if match:
                        out.append((match.group(1), match.group(2)))
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


def _parse_xml(text: str):
    """Parse a manifest as XML, refusing anything carrying a DTD."""
    if "<!DOCTYPE" in text[:4096].upper():
        log.warning("manifest declares a DTD; refusing to expand it")
        return None
    try:
        return ET.fromstring(text)  # noqa: S314 - DTDs refused above
    except ET.ParseError:
        return None


def parse_xml(text: str) -> list[tuple[str, str]]:
    """Maven and MSBuild, which are XML and should be read as XML."""
    root = _parse_xml(text)
    if root is None:
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
    """Formats with no parser that still pin a repository to a ref."""
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


ECOSYSTEMS: tuple[Ecosystem, ...] = (
    Ecosystem("go", ("go.mod",), parse_go),

    Ecosystem("npm", ("package-lock.json", "npm-shrinkwrap.json"), parse_json_pins),
    Ecosystem("npm", ("package.json",), parse_json_deps),
    Ecosystem("npm", ("pnpm-lock.yaml",), parse_yaml),
    Ecosystem("npm", ("yarn.lock", "bun.lock"), parse_pinned_refs),
    Ecosystem("deno", ("deno.json", "deno.jsonc", "import_map.json"), parse_json_deps),

    Ecosystem("python", ("Pipfile.lock",), parse_json_pins),
    Ecosystem("python", ("uv.lock",), parse_toml),
    Ecosystem("python", ("poetry.lock", "pyproject.toml", "Pipfile"), parse_toml),
    Ecosystem("python", ("requirements.txt", "requirements-dev.txt", "constraints.txt",
                         "dev-requirements.txt", "test-requirements.txt"), parse_requirements),
    Ecosystem("python", ("environment.yml", "conda.yaml"), parse_conda),

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
        if any(c.isspace() for c in name):
            continue
        if not any(c.isdigit() for c in str(raw)):
            continue
        ref = classify(name, raw, eco.name)
        if (ref.name, ref.raw) not in seen:
            seen.add((ref.name, ref.raw))
            out.append(ref)
    return out
