"""The configuration surface is one list, and every copy of it agrees."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

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

OS_PROVIDED = {"HOME", "PATH"}

READ_BY_THE_UI_TESTS = {"GS_TEST_EMAIL", "GS_TEST_PASSWORD", "GS_SETUP_TOKEN"}

PUBLISHED_PORTS = {
    "API_HTTP_PORT", "API_PUBLISHED_PORT",
    "MCP_PUBLISHED_PORT", "POSTGRES_PUBLISHED_PORT",
}


def _documented() -> dict[str, str | None]:
    """Every variable named in .env.example, set or commented out."""
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
    """A knob nothing reads is worse than an undocumented one: it is a promise."""
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
    """An example value that differs from the code's default is a second default."""
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
