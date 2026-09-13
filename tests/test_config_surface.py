"""The configuration surface is one list, and every copy of it agrees.

A knob exists in up to four places: the dataclass in :mod:`git_synapse.config`
that reads it, the ``.env.example`` a person edits, the compose file, and the
Helm chart. Nothing keeps those in step on its own, and each way they can
drift misleads a reader in a different direction:

* a variable documented but read by nothing is tuned, restarted, and silently
  does nothing -- which reads as the tool ignoring its own settings;
* a variable read but documented nowhere can only be found by grep;
* an example value that differs from the code's default is a second default,
  and which one is in force depends on whether the reader copied the file.

These tests are cheap and they close all three.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Set by the image, the compose file or the chart -- never by hand. Keeping
#: them out of .env.example is deliberate: it is the file a person edits, and
#: every line in it that cannot sensibly be edited costs the reader attention.
#: A knob belongs here when a deployment sets it for you, not when nobody has
#: got round to documenting it.
INFRASTRUCTURE = {
    "API_HOST", "API_PORT", "CORS_ORIGINS", "WEB_ROOT",   # bound by the image
    "MIRROR_ROOT",                                        # a mounted volume
    "POSTGRES_HOST", "POSTGRES_PORT",                     # the compose network
    "GITHUB_TOKEN_FILE",                                  # a bind-mounted path
    "GITHUB_API_URL",                                     # GitHub Enterprise
    "DB_POOL_SIZE", "DB_POOL_OVERFLOW", "DB_ECHO",        # pool/debug tuning
    "GIT_TIMEOUT",
    "CALL_LOG_BODY_BYTES",                                # call-log capture size
    "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT",              # mcp CLI flag defaults
    "TEST_POSTGRES_DB",                                   # the test suite only
}

#: Provided by the operating system, not by this project. Read like any other
#: variable, but nothing here defines or defaults them.
OS_PROVIDED = {"HOME", "PATH"}

#: Read by the Node UI suites rather than by any Python. Documented in
#: .env.example because a person setting up those runs edits it there.
READ_BY_THE_UI_TESTS = {"GS_TEST_EMAIL", "GS_TEST_PASSWORD", "GS_SETUP_TOKEN"}

#: Consumed by compose itself to pick published host ports; they never reach
#: the application, so no Python reads them.
PUBLISHED_PORTS = {
    "API_HTTP_PORT", "API_PUBLISHED_PORT",
    "MCP_PUBLISHED_PORT", "POSTGRES_PUBLISHED_PORT",
}


def _documented() -> dict[str, str | None]:
    """Every variable named in .env.example, set or commented out.

    A commented-out line documents the variable just as well as a set one --
    that is how the optional credentials are presented -- so both count, but
    only a set line carries a value to compare against the code's default.
    """
    found: dict[str, str | None] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        live = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if live:
            found[live.group(1)] = live.group(2)
            continue
        shown = re.match(r"^#\s*([A-Z][A-Z0-9_]*)=", line)
        if shown:
            found.setdefault(shown.group(1), None)
    return found


def _read_by_the_code() -> set[str]:
    """Every environment variable any module under src/ actually reads."""
    names: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            # config.py's own typed readers: _env_str("NAME", default)
            if isinstance(target, ast.Name) and target.id.startswith("_env_"):
                if node.args and isinstance(node.args[0], ast.Constant):
                    names.add(node.args[0].value)
            # os.environ.get("NAME") / os.getenv("NAME")
            elif (
                isinstance(target, ast.Attribute)
                and target.attr in {"get", "getenv"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                value = node.args[0].value
                if isinstance(value, str) and value.isupper():
                    names.add(value)
        # os.environ["NAME"] and the module-level ENV_KEY = "NAME" constants
        text = path.read_text(encoding="utf-8")
        names.update(re.findall(r'os\.environ\[\s*"([A-Z][A-Z0-9_]*)"', text))
        names.update(re.findall(r'^ENV_KEY\s*=\s*"([A-Z][A-Z0-9_]*)"', text, re.MULTILINE))
    return names


def test_every_documented_variable_is_read_by_something():
    """A knob nothing reads is worse than an undocumented one: it is a promise.

    The seven change-set knobs this catches outlived the model that read them,
    and stayed in the file describing a pipeline stage that no longer runs.
    """
    documented = set(_documented())
    known = _read_by_the_code() | READ_BY_THE_UI_TESTS | PUBLISHED_PORTS
    orphans = sorted(documented - known)
    assert not orphans, (
        f".env.example documents {orphans}, which nothing reads. Delete them, "
        "or add the reader."
    )


def test_every_variable_the_code_reads_is_documented_or_infrastructure():
    """The other direction: a knob only grep can find."""
    undocumented = sorted(
        _read_by_the_code() - set(_documented()) - INFRASTRUCTURE - OS_PROVIDED
    )
    assert not undocumented, (
        f"{undocumented} are read by the code but appear in neither "
        ".env.example nor INFRASTRUCTURE. Document them, or record here that "
        "a deployment sets them."
    )


@pytest.mark.parametrize("name,value", sorted(
    (k, v) for k, v in _documented().items() if v is not None
))
def test_the_example_never_silently_overrides_a_code_default(name, value):
    """An example value that differs from the code's default is a second default.

    Rather than compare parsed text against the source, this sets the variable
    to exactly what the file says and asks whether the resulting Config differs
    from the one built with it unset. If it does, the two disagree, and which
    applies depends on whether the reader copied .env.example to .env.
    """
    if name in PUBLISHED_PORTS | READ_BY_THE_UI_TESTS:
        pytest.skip(f"{name} never reaches Config")

    from git_synapse.config import Config

    previous = os.environ.get(name)
    try:
        os.environ.pop(name, None)
        default = Config()
        os.environ[name] = value
        documented = Config()
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    assert documented == default, (
        f".env.example sets {name}={value!r}, which is not what "
        "git_synapse.config defaults to. Change one to match the other."
    )
