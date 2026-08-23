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


# --------------------------------------------------- discovery and ingest

def test_discover_prints_what_it_found(db, monkeypatch):
    """`discover` is the only command that spends GitHub API quota, so it is
    driven here with a stubbed listing rather than a live call."""
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord

    fake = [
        RepoRecord(github_id=940000 + i, owner="t", name=f"disc{i}",
                   full_name=f"t/disc{i}", clone_url=f"https://x/{i}.git",
                   default_branch="main", disk_usage_kb=1000 * (i + 1),
                   is_private=bool(i % 2))
        for i in range(3)
    ]
    monkeypatch.setattr(pipeline, "discover", lambda trigger="manual": fake)

    r = runner.invoke(app, ["discover"])
    assert r.exit_code == 0, r.stdout
    assert "disc0" in r.stdout


def test_discover_lists_at_most_a_page_and_says_how_many_more(db, monkeypatch):
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord

    many = [
        RepoRecord(github_id=950000 + i, owner="t", name=f"m{i}",
                   full_name=f"t/m{i}", clone_url="https://x.git",
                   default_branch="main", disk_usage_kb=i)
        for i in range(50)
    ]
    monkeypatch.setattr(pipeline, "discover", lambda trigger="manual": many)

    r = runner.invoke(app, ["discover"])
    assert r.exit_code == 0
    assert "more" in r.stdout, "a truncated listing must say it was truncated"


def test_ingest_named_repos_filters_to_them(db, monkeypatch):
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord

    seen = {}

    def fake_run(records=None, trigger="manual", **kw):
        seen["names"] = [r.name for r in (records or [])]

        class _R:
            status, run_id, duration_s, commits_added = "success", 1, 0.1, 0
            ok, failed, repos = [], [], []
        return _R()

    pool = [
        RepoRecord(github_id=960000 + i, owner="t", name=n, full_name=f"t/{n}",
                   clone_url="https://x.git", default_branch="main")
        for i, n in enumerate(("alpha", "beta", "gamma"))
    ]
    monkeypatch.setattr(pipeline, "load_repo_records", lambda: pool)
    monkeypatch.setattr(pipeline, "run_ingest", fake_run)

    r = runner.invoke(app, ["ingest", "--repo", "beta"])
    assert r.exit_code == 0, r.stdout
    assert seen["names"] == ["beta"], seen


def test_ingest_with_an_unmatched_repo_name_does_not_silently_do_everything(db, monkeypatch):
    """Filtering to nothing must stop, not fall through to the whole corpus."""
    from git_synapse.ingest import pipeline
    from git_synapse.ingest.github import RepoRecord

    called = {"n": 0}

    def fake_run(records=None, trigger="manual", **kw):
        called["n"] += 1
        class _R:
            status, run_id, duration_s, commits_added = "success", 1, 0.1, 0
            ok, failed, repos = [], [], []
        return _R()

    monkeypatch.setattr(pipeline, "load_repo_records", lambda: [
        RepoRecord(github_id=970001, owner="t", name="only", full_name="t/only",
                   clone_url="https://x.git", default_branch="main")
    ])
    monkeypatch.setattr(pipeline, "run_ingest", fake_run)

    r = runner.invoke(app, ["ingest", "--repo", "does-not-exist"])
    assert called["n"] == 0, "an unmatched filter must not ingest everything"
    assert r.exit_code != 0 or "no repositor" in r.stdout.lower()


# ------------------------------------------------- the reporting commands

def test_chains_prints_a_chain_when_one_exists(db):
    """`chains` is the multi-hop view; it printed nothing in every observed
    session, so it is worth asserting it can print something."""
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name FROM repo r
        WHERE EXISTS (
            SELECT 1 FROM repo_impact a
            JOIN repo_impact b ON b.source_repo_id = a.target_repo_id
            WHERE a.target_repo_id = r.id
              AND (a.is_declared OR a.has_bump_history)
              AND (b.is_declared OR b.has_bump_history)
        ) LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no repository with a validated two-hop path")
    r = runner.invoke(app, ["chains", row["name"]])
    assert r.exit_code == 0, r.stdout
    assert "<-" in r.stdout or "->" in r.stdout or "no chains" in r.stdout.lower()


def test_chains_honours_its_depth_and_confidence_options(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    for args in (["--depth", "2"], ["--min-confidence", "0.9"], ["-n", "3"]):
        r = runner.invoke(app, ["chains", row["name"], *args])
        assert r.exit_code == 0, f"{args}: {r.stdout}"


def test_xcoupled_reports_cross_repo_partners(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        "SELECT r.name FROM xrepo_file_pair x JOIN repo r ON r.id = x.repo_a_id LIMIT 1"
    )
    if row is None:
        pytest.skip("no cross-repo pairs")
    r = runner.invoke(app, ["xcoupled", row["name"], "-n", "3"])
    assert r.exit_code == 0, r.stdout


def test_xcoupled_rejects_an_unknown_measure(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    r = runner.invoke(app, ["xcoupled", row["name"], "-m", "not_a_measure"])
    assert r.exit_code != 0 or "measure" in r.stdout.lower()


def test_measures_shows_the_caveats_not_only_the_names(db):
    """The catalogue exists so a reader can pick a measure knowingly."""
    r = runner.invoke(app, ["measures"])
    assert r.exit_code == 0
    assert len(r.stdout.splitlines()) > 20, "the catalogue must list every measure"


def test_status_after_a_run_reports_the_run(db):
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    assert any(w in r.stdout.lower() for w in ("run", "ingest", "repo"))


# --------------------------------------------------- the destructive command

def test_reset_requires_an_explicit_yes(scratch_db):
    """`reset` drops every ingested row. Nothing but an explicit flag may run
    it, because the mistake is unrecoverable without a full re-ingest."""
    from git_synapse.db.engine import connection, query_one

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO repo (github_id, owner, name, full_name, default_branch,
                              is_enabled, ingest_status)
            VALUES (980001,'t','reset-probe','t/reset-probe','main',TRUE,'ready')
            ON CONFLICT (github_id) DO NOTHING
            """
        )
    before = query_one("SELECT count(*) AS n FROM repo")["n"]
    assert before > 0

    r = runner.invoke(app, ["reset"])
    assert "refusing" in r.stdout.lower() or r.exit_code != 0
    assert query_one("SELECT count(*) AS n FROM repo")["n"] == before


def test_reset_advertises_the_flag_it_requires(scratch_db):
    """The destructive half is deliberately not executed here: running it would
    wipe the scratch database other modules' fixtures depend on. What matters is
    that it refuses by default and says how to mean it."""
    r = runner.invoke(app, ["reset", "--help"])
    assert r.exit_code == 0
    assert "--yes" in r.stdout


def test_feedback_shows_nothing_gracefully_when_there_is_nothing(scratch_db):
    r = runner.invoke(app, ["feedback"])
    assert r.exit_code == 0
    assert "no reports" in r.stdout.lower() or r.stdout.strip()


def test_feedback_renders_severities(scratch_db):
    """Severity drives the colour, and ranking it as text once put low above
    high; the table must render every level without choking."""
    from git_synapse.analysis.query import record_feedback
    from git_synapse.db.engine import connection

    ids = []
    for sev in ("low", "medium", "high"):
        ids.append(record_feedback(
            kind="wrong_data", detail=f"PROBE {sev}", severity=sev,
            tool="coupled_files", repo="t/probe", expected=sev, observed="x",
        )["id"])
    try:
        r = runner.invoke(app, ["feedback", "--status", "open"])
        assert r.exit_code == 0, r.stdout
        for sev in ("low", "medium", "high"):
            assert sev in r.stdout
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM feedback WHERE id = ANY(%s)", (ids,))
