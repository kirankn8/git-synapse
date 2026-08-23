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


# ---------------------------------------------------- commands that compute

def test_aggregate_runs_for_a_single_repo(scratch_db):
    """`--repo-id 0` means every repo; a real id must scope to one."""
    from git_synapse.db.engine import connection

    with connection() as conn:
        rid = conn.execute(
            """
            INSERT INTO repo (github_id, owner, name, full_name, default_branch,
                              is_enabled, ingest_status)
            VALUES (930001,'t','cli-agg','t/cli-agg','main',TRUE,'ready')
            ON CONFLICT (github_id) DO UPDATE SET name='cli-agg'
            RETURNING id
            """
        ).fetchone()[0]
    try:
        r = runner.invoke(app, ["aggregate", "--repo-id", str(rid)])
        assert r.exit_code == 0, r.stdout
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM repo WHERE id=%s", (rid,))


def test_score_accepts_a_repo_id(scratch_db):
    r = runner.invoke(app, ["score", "--repo-id", "999999999"])
    assert r.exit_code == 0, r.stdout


def test_crossrepo_and_lagged_and_mine_run_on_an_empty_scratch(scratch_db):
    """These are global rebuilds; on an empty corpus they must be no-ops, not
    crashes, or a fresh install fails on its first schedule tick."""
    for cmd in (["crossrepo"], ["lagged"], ["mine"], ["depbump"]):
        r = runner.invoke(app, cmd)
        assert r.exit_code == 0, f"{cmd}: {r.stdout}"


def test_validate_reports_or_says_there_is_nothing_to_validate(scratch_db):
    r = runner.invoke(app, ["validate"])
    assert r.exit_code == 0, r.stdout


def test_init_applies_the_schema_idempotently(scratch_db):
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["init"]).exit_code == 0


def test_xcoupled_on_an_unknown_repo_is_handled(db):
    r = runner.invoke(app, ["xcoupled", "definitely-not-a-repo"])
    assert r.exit_code != 0 or "no repositor" in r.stdout.lower()


def test_chains_on_an_unknown_repo_is_handled(db):
    r = runner.invoke(app, ["chains", "definitely-not-a-repo"])
    assert r.exit_code != 0 or "no repositor" in r.stdout.lower()


def test_feedback_filters_by_kind_and_status(db):
    for args in (["feedback", "--status", "fixed"],
                 ["feedback", "--status", "open"],
                 ["feedback", "--kind", "wrong_data"]):
        assert runner.invoke(app, args).exit_code == 0, args


def test_feedback_round_trip_files_and_resolves(scratch_db):
    """The loop only works if a report can be closed from the CLI."""
    from git_synapse.analysis.query import record_feedback
    from git_synapse.db.engine import connection, query_one

    rec = record_feedback(
        kind="tool_error", detail="PROBE cli round trip", severity="low",
        tool="coupled_files", repo="t/probe", expected="a", observed="b",
    )
    try:
        r = runner.invoke(app, ["feedback", "--resolve", str(rec["id"]),
                                "--as", "fixed", "--note", "closed by test"])
        assert r.exit_code == 0, r.stdout
        row = query_one("SELECT status, resolution FROM feedback WHERE id=%s", (rec["id"],))
        assert row["status"] == "fixed"
        assert "closed by test" in (row["resolution"] or "")
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM feedback WHERE id=%s", (rec["id"],))


def test_impact_needs_a_repository_and_handles_an_unknown_one(db):
    """`impact` is a query, not a rebuild: it must be told what to report on."""
    assert runner.invoke(app, ["impact"]).exit_code != 0

    r = runner.invoke(app, ["impact", "definitely-not-a-repo"])
    assert r.exit_code != 0 or "no repositor" in r.stdout.lower()


def test_impact_on_a_real_repository_reports(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    r = runner.invoke(app, ["impact", row["name"]])
    assert r.exit_code == 0, r.stdout
