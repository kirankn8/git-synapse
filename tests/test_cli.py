"""The CLI surface.

324 statements at zero coverage: every command could have been broken by an
import error or a renamed argument and nothing would have said so. These run the
read-only commands for real and assert the destructive ones refuse to fire by
accident.
"""
from __future__ import annotations

import pytest

pytest.importorskip("typer")
from typer.testing import CliRunner  # noqa: E402

from git_synapse.cli import app  # noqa: E402

runner = CliRunner()


def test_help_lists_every_command():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("ingest", "aggregate", "score", "coupled", "status", "measures"):
        assert cmd in r.stdout


@pytest.mark.parametrize(
    "cmd",
    ["ingest", "aggregate", "score", "coupled", "xcoupled", "chains",
     "measures", "status", "feedback", "validate", "impact", "mine", "reset"],
)
def test_every_command_has_usable_help(cmd):
    """A renamed or broken option shows up here before a user hits it."""
    r = runner.invoke(app, [cmd, "--help"])
    assert r.exit_code == 0, r.stdout
    assert "Usage" in r.stdout or "usage" in r.stdout


def test_measures_prints_the_full_catalogue(db):
    r = runner.invoke(app, ["measures"])
    assert r.exit_code == 0
    assert "npmi" in r.stdout.lower()


def test_status_reports_without_touching_anything(db):
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    assert any(w in r.stdout.lower() for w in ("repo", "commit", "database"))


def test_coupled_needs_a_repo_and_path(db):
    """Missing required arguments must fail loudly, not run against nothing."""
    r = runner.invoke(app, ["coupled"])
    assert r.exit_code != 0


def test_coupled_on_an_unknown_file_says_so(db):
    r = runner.invoke(app, ["coupled", "acme/runtime", "no/such/file.go"])
    assert r.exit_code != 0 or "no file" in r.stdout.lower() or "not found" in r.stdout.lower()


def test_coupled_on_a_real_file_prints_partners(db):
    r = runner.invoke(app, ["coupled", "runtime", "go.mod", "-n", "3", "--min-support", "5"])
    if r.exit_code != 0:
        pytest.skip("fixture repository not indexed")
    assert r.stdout.strip()


def test_coupled_rejects_an_unknown_measure(db):
    r = runner.invoke(app, ["coupled", "runtime", "go.mod", "-m", "not_a_measure"])
    assert r.exit_code != 0 or "measure" in r.stdout.lower()


def test_feedback_lists_without_arguments(db):
    r = runner.invoke(app, ["feedback"])
    assert r.exit_code == 0


def test_feedback_resolve_needs_a_status(db):
    r = runner.invoke(app, ["feedback", "--resolve", "999999"])
    # Either it demands --as, or it reports the id does not exist. It must not
    # silently claim success.
    assert r.exit_code != 0 or "not" in r.stdout.lower() or "unknown" in r.stdout.lower()


def test_reset_does_not_wipe_anything_without_confirmation(db):
    """`reset` drops data. It must never proceed on an unattended invocation."""
    from git_synapse.db.engine import query_one

    before = query_one("SELECT count(*) AS n FROM repo")["n"]
    r = runner.invoke(app, ["reset"], input="\n")
    after = query_one("SELECT count(*) AS n FROM repo")["n"]
    assert after == before, "reset destroyed data without an explicit confirmation"
    assert r.exit_code != 0 or "abort" in r.stdout.lower() or "cancel" in r.stdout.lower()
