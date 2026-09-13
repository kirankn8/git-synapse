"""The documents must describe what the code currently does.

Prose rots silently. A README that lists twelve MCP tools when fourteen are
registered is not a small inaccuracy: it is the file an agent's operator reads
to decide what to wire up, and the two it omits are the two nobody uses.

Only claims that are mechanically checkable live here -- a count, a name, a
path. Judgement about whether an explanation is still *right* is not something
a test can hold, and pretending otherwise would make this file a place where
assertions are weakened until they pass.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "DESIGN.md").read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def test_the_readme_lists_every_mcp_tool_and_no_others():
    from git_synapse.mcp import server

    section = _section(README, "### Fourteen tools", "### The workflow")
    listed = set(re.findall(r"^\| `(\w+)`", section, re.MULTILINE))
    assert listed == set(server.server._tool_manager._tools)


@pytest.mark.parametrize("name", ["README.md", "DESIGN.md"])
def test_the_stated_tool_count_matches_the_registry(name):
    text = README if name == "README.md" else DESIGN
    from git_synapse.mcp import server

    n = len(server.server._tool_manager._tools)
    words = {12: "Twelve", 13: "Thirteen", 14: "Fourteen", 15: "Fifteen", 16: "Sixteen"}
    stale = [m for m in re.findall(r"\b(\w+) tools\b", text)
             if m in words.values() and m != words.get(n)]
    assert not stale, f"{name} says {stale}, but {n} tools are registered"


def _schema_tables() -> list[str]:
    from git_synapse.db.schema import metadata

    return list(metadata.tables)


def test_design_lists_every_table_in_the_schema():
    """The section is titled "Every table at a glance", so it has to be every
    one: a table nobody documents is a table nobody knows they can query."""
    schema = set(_schema_tables())
    section = _section(DESIGN, "## Every table at a glance", "## How it works")
    listed = set(re.findall(r"^\|\s*\*{0,2}`(\w+)`", section, re.MULTILINE))
    assert schema - listed == set(), f"undocumented tables: {sorted(schema - listed)}"
    assert listed - schema == set(), f"documented but absent: {sorted(listed - schema)}"


def test_design_states_the_right_number_of_tables():
    schema = _schema_tables()
    words = {28: "Twenty-eight", 29: "Twenty-nine", 30: "Thirty",
             31: "Thirty-one", 32: "Thirty-two", 33: "Thirty-three",
             34: "Thirty-four", 35: "Thirty-five"}
    assert f"{words[len(schema)]} tables." in DESIGN


def test_the_measure_count_is_the_registry_size():
    """Every "N measures" in the docs, and in the prose an agent is handed, has
    to be the number actually computed.

    The registry holds 31: 29 symmetric ones plus two directional. Scoring
    persists all of them, so "31" is the number for anything a reader can see,
    and the docs said 29 while the pair page drew 31 bars.
    """
    from git_synapse.mcp import server
    from git_synapse.stats import registry

    n = len(registry.ALL_KEYS)
    root = Path(__file__).resolve().parents[1]
    sources = {
        "README.md": README,
        "DESIGN.md": DESIGN,
        "the MCP instructions": server.INSTRUCTIONS,
        # The surfaces a person or an agent actually reads the number on. The
        # symmetric subset is 29 and is stored nowhere, so a count that is not
        # the registry size sends someone looking for two missing bars.
        "the skill an agent is handed": (
            root / "skills/git-synapse-mcp/SKILL.md").read_text(encoding="utf-8"),
        "the web UI": (root / "web/static/app.js").read_text(encoding="utf-8")
        + (root / "web/index.html").read_text(encoding="utf-8"),
        "the Makefile help": (root / "Makefile").read_text(encoding="utf-8"),
    }
    for name, text in sources.items():
        # A count spelled out as its parts -- "29 association measures + 2
        # directional" -- is the registry described exactly, not a stale total.
        stated = {
            int(m) for m in re.findall(
                r"\b(\d+) (?:association )?measures\b(?!\s*\+\s*\d+\s*directional)",
                text,
            )
        }
        assert stated <= {n}, f"{name} says {sorted(stated)} measures; there are {n}"


@pytest.mark.parametrize("name", ["README.md", "DESIGN.md"])
def test_every_ui_path_named_in_the_docs_is_a_real_route(name):
    """A document that sends a reader to a page that does not exist is worse
    than one that says nothing.

    This matches the *shape* of a path against the router, which is as far as a
    static check goes: `/insights/nowhere` fits `/insights/:section` and passes
    here, even though the handler renders a not-found. It catches the case that
    actually happens -- a route renamed or removed under a document that still
    names it.
    """
    text = README if name == "README.md" else DESIGN
    app = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
    routes = [r.rstrip("/").split("/") for r in re.findall(r"^on\('([^']+)'", app, re.MULTILINE)]

    def served(path: str) -> bool:
        parts = path.split("?", maxsplit=1)[0].rstrip("/").split("/")
        return any(len(r) == len(parts)
                   and all(a.startswith(":") or a == b
                           for a, b in zip(r, parts, strict=True))
                   for r in routes)

    # Only paths that look like UI routes: not the API, not files on disk, and
    # not the MCP transport, none of which the single-page router serves.
    named = {p for p in re.findall(r"`(/[a-z][\w/{}.-]*)`", text)
             if not p.startswith(("/api", "/auth", "/etc", "/abs", "/app",
                                  "/mcp", "/work", "/usr"))}
    dead = [p for p in sorted(named) if not served(p)]
    assert not dead, f"{name} sends the reader to {dead}, which no route serves"
