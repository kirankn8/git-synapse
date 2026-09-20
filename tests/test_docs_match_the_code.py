"""The documents must describe what the code currently does."""
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
    """The section is titled "Every table at a glance", so it has to be every one: a table nobody documents is a table nobody knows they can query."""
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
    """Every "N measures" in the docs, and in the prose an agent is handed, has to be the number actually computed."""
    from git_synapse.mcp import server
    from git_synapse.stats import registry

    n = len(registry.ALL_KEYS)
    root = Path(__file__).resolve().parents[1]
    sources = {
        "README.md": README,
        "DESIGN.md": DESIGN,
        "the MCP instructions": server.INSTRUCTIONS,
        "the skill an agent is handed": (
            root / "skills/git-synapse-mcp/SKILL.md").read_text(encoding="utf-8"),
        "the web UI": (root / "web/static/app.js").read_text(encoding="utf-8")
        + (root / "web/index.html").read_text(encoding="utf-8"),
        "the Makefile help": (root / "Makefile").read_text(encoding="utf-8"),
    }
    for name, text in sources.items():
        stated = {
            int(m) for m in re.findall(
                r"\b(\d+) (?:association )?measures\b(?!\s*\+\s*\d+\s*directional)",
                text,
            )
        }
        assert stated <= {n}, f"{name} says {sorted(stated)} measures; there are {n}"


@pytest.mark.parametrize("name", ["README.md", "DESIGN.md"])
def test_every_ui_path_named_in_the_docs_is_a_real_route(name):
    """A document that sends a reader to a page that does not exist is worse than one that says nothing."""
    text = README if name == "README.md" else DESIGN
    app = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
    routes = [r.rstrip("/").split("/") for r in re.findall(r"^on\('([^']+)'", app, re.MULTILINE)]

    def served(path: str) -> bool:
        parts = path.split("?", maxsplit=1)[0].rstrip("/").split("/")
        return any(len(r) == len(parts)
                   and all(a.startswith(":") or a == b
                           for a, b in zip(r, parts, strict=True))
                   for r in routes)

    named = {p for p in re.findall(r"`(/[a-z][\w/{}.-]*)`", text)
             if not p.startswith(("/api", "/auth", "/etc", "/abs", "/app",
                                  "/mcp", "/work", "/usr"))}
    dead = [p for p in sorted(named) if not served(p)]
    assert not dead, f"{name} sends the reader to {dead}, which no route serves"


def _documented_cli_commands() -> set[str]:
    """Every `cli <command>` the README tells a reader to run."""
    section = README[README.index("## CLI and development"):]
    found = set()
    for line in section.splitlines():
        match = re.search(r"\bcli ([a-z-]+)(?: ([a-z-]+))?", line)
        if not match:
            continue
        group, sub = match.group(1), match.group(2)
        found.add(f"{group} {sub}" if sub and not sub.startswith("-") else group)
    return found


def test_every_cli_command_in_the_readme_exists():
    """The README listed eleven commands that had been deleted, and nothing noticed."""
    from git_synapse import cli

    def named(value, fallback=None):
        # typer leaves `name` as a DefaultPlaceholder when the sub-app carries it.
        return value if isinstance(value, str) else fallback

    real = {c.name for c in cli.app.registered_commands if isinstance(c.name, str)}
    for group in cli.app.registered_groups:
        label = named(group.name, named(group.typer_instance.info.name))
        if not label:
            continue
        real.add(label)
        for command in group.typer_instance.registered_commands:
            if isinstance(command.name, str):
                real.add(f"{label} {command.name}")

    documented = _documented_cli_commands()
    assert documented, "the README should show how to run the CLI"
    unknown = sorted(documented - real)
    assert not unknown, (
        f"README.md documents {unknown}, which the CLI does not provide. "
        f"It offers {sorted(real)}."
    )
