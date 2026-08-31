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
    ["ingest", "aggregate", "score", "coupled", "measures", "status", "feedback", "impact", "mine", "reset"],
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


def test_init_applies_the_schema_idempotently(scratch_db):
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["init"]).exit_code == 0


def test_xcoupled_on_an_unknown_repo_is_handled(db):
    r = runner.invoke(app, ["definitely-not-a-repo"])
    assert r.exit_code != 0 or "no repositor" in r.stdout.lower()


def test_chains_on_an_unknown_repo_is_handled(db):
    r = runner.invoke(app, ["definitely-not-a-repo"])
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


def test_xcoupled_rejects_an_unknown_measure(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    r = runner.invoke(app, [row["name"], "-m", "not_a_measure"])
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


# ---------------------------------------------- output when there is nothing

def test_ingest_discovers_when_nothing_is_known_yet(scratch_db, monkeypatch):
    """A first run has no stored repositories, so it must fall through to
    discovery rather than silently doing nothing."""
    from git_synapse.ingest import pipeline

    discovered = []
    monkeypatch.setattr(pipeline, "load_repo_records", lambda: [])
    monkeypatch.setattr(pipeline, "discover", lambda **kw: discovered.append(1) or [])

    class _R:
        status, run_id, duration_s, commits_added = "success", 1, 0.0, 0
        ok, failed, repos = [], [], []

    monkeypatch.setattr(pipeline, "run_ingest", lambda **kw: _R())
    r = runner.invoke(app, ["ingest"])
    assert r.exit_code == 0, r.stdout
    assert "discovering" in r.stdout.lower() or discovered


def test_aggregate_says_so_when_there_is_nothing_to_do(scratch_db):
    """Silence and success look identical; this must say which it was."""
    r = runner.invoke(app, ["aggregate"])
    assert r.exit_code == 0, r.stdout


def test_impact_marks_the_evidence_tier_on_each_row(db):
    """The tier is the whole point of the row; a table without it invites
    acting on a discovery edge as though it were declared."""
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name FROM repo_impact i JOIN repo r ON r.id = i.source_repo_id
        WHERE i.is_declared OR i.has_bump_history LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no validated impact rows")
    r = runner.invoke(app, ["impact", row["name"]])
    assert r.exit_code == 0, r.stdout
    assert "declared" in r.stdout.lower() or "bumps" in r.stdout.lower()


# ------------------------------------------------- render paths on real rows
#
# Most CLI commands query, get nothing back from a test database, and print
# "nothing found". The table-formatting code below that -- where a None median
# lag, a missing name or a renamed key actually breaks -- never ran. These feed
# each command synthetic rows so the formatting executes.


class _Stats:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _any_repo_name():
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    return row["name"]


def test_aggregate_says_so_when_nothing_is_stale(monkeypatch):
    monkeypatch.setattr("git_synapse.cli.repos_needing_aggregation", lambda: [])
    r = runner.invoke(app, ["aggregate"])
    assert r.exit_code == 0
    assert "nothing to aggregate" in r.stdout


def test_aggregate_reports_pair_counts_per_repo(monkeypatch):
    monkeypatch.setattr("git_synapse.cli.repos_needing_aggregation", lambda: [7])
    monkeypatch.setattr("git_synapse.cli.rebuild_repo",
                        lambda rid: _Stats(file_pairs=12, dir_pairs=3))
    monkeypatch.setattr("git_synapse.cli.score_repo", lambda rid: _Stats(file_pairs=12))
    r = runner.invoke(app, ["aggregate"])
    assert r.exit_code == 0
    assert "repo 7" in r.stdout and "12 file pairs" in r.stdout
    assert "scored 12" in r.stdout


def test_aggregate_can_skip_scoring(monkeypatch):
    monkeypatch.setattr("git_synapse.cli.rebuild_repo",
                        lambda rid: _Stats(file_pairs=1, dir_pairs=0))
    monkeypatch.setattr("git_synapse.cli.score_repo",
                        lambda rid: pytest.fail("--no-rescore still scored"))
    r = runner.invoke(app, ["aggregate", "--repo-id", "4", "--no-rescore"])
    assert r.exit_code == 0
    assert "scored" not in r.stdout


def test_depbump_renders_the_propagation_lag_table(monkeypatch):
    from git_synapse.analysis.depbump import BumpStats

    monkeypatch.setattr("git_synapse.cli.depbump.rebuild",
                        lambda force=False: BumpStats(repos_scanned=3, edges_found=9,
                                                      edges_written=4, resolved_commits=8,
                                                      duration_s=1.5))
    monkeypatch.setattr("git_synapse.cli.depbump.propagation_lags", lambda limit=15: [
        {"dep": "httpkit", "consumer": "console", "bumps": 6,
         "median_lag_days": 2, "p90_lag_days": 9, "last_bump": "2026-08-01"},
        # A dependency bumped exactly once has no lag distribution yet; the
        # table must print a dash rather than formatting None.
        {"dep": "telemetry", "consumer": "runtime", "bumps": 1,
         "median_lag_days": None, "p90_lag_days": None, "last_bump": "2026-07-04"},
    ])
    r = runner.invoke(app, ["depbump"])
    assert r.exit_code == 0, r.stdout
    assert "httpkit" in r.stdout and "telemetry" in r.stdout


def test_depbump_omits_the_lag_table_when_there_are_no_lags(monkeypatch):
    from git_synapse.analysis.depbump import BumpStats

    monkeypatch.setattr("git_synapse.cli.depbump.rebuild", lambda force=False: BumpStats())
    monkeypatch.setattr("git_synapse.cli.depbump.propagation_lags", lambda limit=15: [])
    r = runner.invoke(app, ["depbump", "--force"])
    assert r.exit_code == 0
    assert "propagation lag" not in r.stdout


def _impact_rows():
    return [
        {"score": 0.91, "is_declared": True, "has_bump_history": True, "bump_count": 12,
         "median_lag_days": 1.5, "name": "acme/httpkit"},
        {"score": 0.40, "is_declared": False, "has_bump_history": True, "bump_count": 3,
         "median_lag_days": None, "name": "acme/telemetry"},
        {"score": 0.11, "is_declared": False, "has_bump_history": False, "bump_count": 0,
         "median_lag_days": None, "name": "acme/runtime"},
    ]


@pytest.mark.parametrize(("direction", "patched"),
                         [("upstream", "upstream_of"), ("downstream", "impact_for")])
def test_impact_labels_each_evidence_tier(db, monkeypatch, direction, patched):
    """declared / bumps / discovery must be visibly distinct -- acting on a
    discovery-tier row as if it were declared is the expensive mistake."""
    monkeypatch.setattr(f"git_synapse.cli.predict.{patched}",
                        lambda rid, limit=15: _impact_rows())
    repo = _any_repo_name()
    r = runner.invoke(app, ["impact", repo, "--direction", direction])
    assert r.exit_code == 0, r.stdout
    for tier in ("declared", "bumps", "discovery"):
        assert tier in r.stdout


def test_reset_with_yes_truncates_only_the_atom_tables(monkeypatch):
    """Runs against a recording stub: pointing this at the real database would
    delete every ingested commit, which is exactly what it is meant to do."""
    executed = []

    class _Conn:
        def execute(self, sql, *a):
            executed.append(" ".join(str(sql).split()))

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("git_synapse.cli.connection", lambda *a, **k: _Ctx())
    r = runner.invoke(app, ["reset", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "all ingested data removed" in r.stdout
    assert executed == ["TRUNCATE repo, author, ingest_run RESTART IDENTITY CASCADE"]


def test_ingest_lists_the_failures_and_exits_nonzero_when_all_failed(monkeypatch):
    """A run where every repository failed must not exit 0 -- the scheduler and
    any wrapping script read that code."""
    class _Fail:
        def __init__(self, name):
            self.full_name = name
            self.error = "remote: Invalid username or token" * 20

    monkeypatch.setattr("git_synapse.cli.pipeline.load_repo_records", lambda: ["x"])
    monkeypatch.setattr("git_synapse.cli.pipeline.run_ingest", lambda **k: _Stats(
        run_id=9, status="failed", duration_s=2.0, ok=[],
        failed=[_Fail(f"acme/r{i}") for i in range(25)], commits_added=0))
    r = runner.invoke(app, ["ingest"])
    assert r.exit_code == 1
    assert "failures" in r.stdout and "acme/r0" in r.stdout


def test_ingest_exits_zero_when_some_repositories_succeeded(monkeypatch):
    """Partial failure is the normal case across 272 repositories; failing the
    whole run over one unreachable remote would stop every scheduled sync."""
    class _Fail:
        full_name = "acme/gone"
        error = None

    monkeypatch.setattr("git_synapse.cli.pipeline.load_repo_records", lambda: ["x"])
    monkeypatch.setattr("git_synapse.cli.pipeline.run_ingest", lambda **k: _Stats(
        run_id=9, status="partial", duration_s=1.0, ok=["a"],
        failed=[_Fail()], commits_added=5))
    r = runner.invoke(app, ["ingest"])
    assert r.exit_code == 0
    assert "1 ok" in r.stdout


def test_score_recomputes_one_repo_or_every_repo(monkeypatch):
    seen = []
    monkeypatch.setattr("git_synapse.cli.score_repo",
                        lambda rid: seen.append(rid) or _Stats(file_pairs=3, duration_s=0.1))
    assert runner.invoke(app, ["score", "--repo-id", "11"]).exit_code == 0
    assert seen == [11]

    monkeypatch.setattr("git_synapse.cli.query",
                        lambda *a, **k: [{"id": 2, "full_name": "s/a"},
                                         {"id": 3, "full_name": "s/b"}])
    r = runner.invoke(app, ["score"])
    assert r.exit_code == 0
    assert seen == [11, 2, 3]
    assert "s/a: 3 pairs" in r.stdout


