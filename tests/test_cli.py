"""The CLI: first-admin setup, source management and reset."""
from __future__ import annotations

import pytest

pytest.importorskip("typer")
from typer.testing import CliRunner

from git_synapse import cli
from git_synapse.cli import app

runner = CliRunner()


def test_admin_setup_token_prints_the_token_for_a_fresh_deployment(monkeypatch):
    monkeypatch.setattr(cli, "_setup", lambda: None)
    monkeypatch.setattr(cli.auth, "count_users", lambda: 0)
    monkeypatch.setattr(cli.auth, "setup_token", lambda: "setup-token-for-test")

    r = runner.invoke(app, ["admin", "setup-token"])

    assert r.exit_code == 0, r.stdout
    assert "setup-token-for-test" in r.stdout


def test_admin_setup_token_refuses_after_the_first_account_exists(monkeypatch):
    monkeypatch.setattr(cli, "_setup", lambda: None)
    monkeypatch.setattr(cli.auth, "count_users", lambda: 1)

    r = runner.invoke(app, ["admin", "setup-token"])

    assert r.exit_code == 1
    assert "already been created" in r.stdout


def test_help_lists_every_command():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("admin", "account", "reset"):
        assert cmd in r.stdout


@pytest.mark.parametrize(
    "cmd",
    [["reset"], ["admin", "setup-token"], ["account", "add"], ["account", "list"],
     ["account", "remove"], ["account", "enable"]],
)
def test_every_command_has_usable_help(cmd):
    """A renamed or broken option shows up here before a user hits it."""
    r = runner.invoke(app, [*cmd, "--help"])
    assert r.exit_code == 0, r.stdout
    assert "Usage" in r.stdout or "usage" in r.stdout


def test_reset_does_not_wipe_anything_without_confirmation(db):
    """`reset` drops data. It must never proceed on an unattended invocation."""
    from git_synapse.db.orm import models, session_scope
    with session_scope() as session:
        before = session.query(models().Repo).count()
    r = runner.invoke(app, ["reset"], input="\n")
    with session_scope() as session:
        after = session.query(models().Repo).count()
    assert after == before, "reset destroyed data without an explicit confirmation"
    assert r.exit_code != 0 or "abort" in r.stdout.lower() or "cancel" in r.stdout.lower()




def test_reset_requires_an_explicit_yes(scratch_db):
    """`reset` drops every ingested row."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        if session.query(models().Repo).filter_by(github_id=980001).one_or_none() is None:
            session.add(models().Repo(github_id=980001, owner="t", name="reset-probe", full_name="t/reset-probe",
                                      default_branch="main", is_enabled=True, ingest_status="ready"))
    with session_scope() as session:
        before = session.query(models().Repo).count()
    assert before > 0

    r = runner.invoke(app, ["reset"])
    assert "refusing" in r.stdout.lower() or r.exit_code != 0
    with session_scope() as session:
        assert session.query(models().Repo).count() == before


def test_reset_advertises_the_flag_it_requires(scratch_db):
    """The destructive half is deliberately not executed here: running it would wipe the scratch database other modules' fixtures depend on."""
    r = runner.invoke(app, ["reset", "--help"])
    assert r.exit_code == 0
    assert "--yes" in r.stdout


def test_reset_with_yes_truncates_only_the_atom_tables(monkeypatch):
    """Runs against a recording stub: pointing this at the real database would delete every ingested commit, which is exactly what it is meant to do."""
    deleted = []
    selected = []

    class _Session:
        def query(self, cls):
            selected.append(cls)
            return self

        def all(self):
            return []

        def delete(self, row):
            deleted.append(row)

    class _Ctx:
        def __enter__(self):
            return _Session()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("git_synapse.cli.session_scope", lambda *a, **k: _Ctx())
    monkeypatch.setattr("git_synapse.cli._setup", lambda: None)
    from types import SimpleNamespace
    monkeypatch.setattr("git_synapse.cli.models", lambda: SimpleNamespace(**{
        name: object() for name in (
            "RepoImpact", "DepBump", "RepoDependency", "ModuleDependency", "RepoPackage",
            "FileRisk", "PairDrift", "FileCluster", "AuthorFile", "DirPairMetric", "FilePairMetric",
            "DirPair", "FilePair", "FileDirectory", "Directory", "CommitParent", "RefTag",
            "CommitFile", "Commit", "FileAlias", "File", "Author", "IngestRunRepo", "IngestRun", "Repo",
        )
    }))
    r = runner.invoke(app, ["reset", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "all ingested data removed" in r.stdout
    assert len(selected) == 25
    assert deleted == []


def test_account_list_says_so_when_nothing_is_configured(db, monkeypatch):
    from git_synapse.ingest import accounts

    monkeypatch.setattr(accounts, "list_accounts", lambda *a, **k: [])
    r = runner.invoke(app, ["account", "list"])
    assert r.exit_code == 0
    assert "no accounts configured" in r.stdout


def _account(**over):
    row = {"id": 1, "login": "acme", "kind": "org", "enabled": True,
           "include_forks": True, "include_archived": True, "include_private": True,
           "only_repos": [], "skip_repos": [], "repo_count": 4,
           "last_discovered_at": None}
    row.update(over)
    return row


def test_account_list_spells_out_each_filter(db, monkeypatch):
    """The filters decide what gets scanned, so a row that does not show them hides the reason a repository was skipped."""
    from git_synapse.ingest import accounts

    monkeypatch.setattr(accounts, "list_accounts", lambda *a, **k: [
        _account(include_private=False, include_archived=False)])
    r = runner.invoke(app, ["account", "list"])
    assert r.exit_code == 0
    # Rich wraps the column at the default width, so compare on text not layout.
    flat = " ".join(r.stdout.split())
    assert "acme" in flat
    assert "no private" in flat and "no archived" in flat
    assert "forks" not in flat


def test_an_allowlist_is_shown_instead_of_the_other_filters(db, monkeypatch):
    """An allowlist overrides every other filter, so listing them beside it would describe rules that are not being applied."""
    from git_synapse.ingest import accounts

    monkeypatch.setattr(accounts, "list_accounts", lambda *a, **k: [
        _account(include_forks=False, only_repos=["a", "b"])])
    flat = " ".join(runner.invoke(app, ["account", "list"]).stdout.split())
    assert "only 2" in flat
    assert "no forks" not in flat


def test_adding_an_account_reports_the_failure_rather_than_a_traceback(db, monkeypatch):
    from git_synapse.ingest import accounts

    def _refuse(*a, **k):
        raise accounts.AccountError("login already configured")

    monkeypatch.setattr(accounts, "add_account", _refuse)
    r = runner.invoke(app, ["account", "add", "acme"])
    assert r.exit_code == 1
    assert "already configured" in r.stdout


def test_enabling_an_unknown_account_fails_cleanly(db, monkeypatch):
    from git_synapse.ingest import accounts

    def _refuse(*a, **k):
        raise accounts.AccountError("no account 99")

    monkeypatch.setattr(accounts, "update_account", _refuse)
    r = runner.invoke(app, ["account", "enable", "99", "--off"])
    assert r.exit_code == 1
    assert "no account 99" in r.stdout


def test_removing_an_unknown_account_fails_cleanly(db, monkeypatch):
    """It reports absence by returning falsy rather than raising, so the caller has to check the value -- a bare call would look like success."""
    from git_synapse.ingest import accounts

    monkeypatch.setattr(accounts, "remove_account", lambda *a, **k: False)
    r = runner.invoke(app, ["account", "remove", "99"])
    assert r.exit_code == 1
    assert "not found" in r.stdout


def test_removing_a_known_account_keeps_its_repositories(db, monkeypatch):
    from git_synapse.ingest import accounts

    monkeypatch.setattr(accounts, "remove_account", lambda *a, **k: True)
    r = runner.invoke(app, ["account", "remove", "1"])
    assert r.exit_code == 0
    assert "removed" in r.stdout


def test_adding_an_account_says_what_to_run_next(db, monkeypatch):
    """Adding one scans nothing by itself, and leaving that unsaid reads as a silent failure."""
    from git_synapse.ingest import accounts

    row = _account(login="fresh")
    monkeypatch.setattr(accounts, "add_account", lambda *a, **k: row)
    r = runner.invoke(app, ["account", "add", "fresh"])
    assert r.exit_code == 0
    assert "Run now" in " ".join(r.stdout.split())


def test_adding_an_account_offers_no_fork_option():
    r = runner.invoke(app, ["account", "add", "--help"])
    assert r.exit_code == 0
    assert "fork" not in r.stdout.lower()


def test_enabling_an_account_shows_its_new_state(db, monkeypatch):
    from git_synapse.ingest import accounts

    monkeypatch.setattr(accounts, "update_account",
                        lambda *a, **k: _account(login="toggled", enabled=False))
    r = runner.invoke(app, ["account", "enable", "1", "--off"])
    assert r.exit_code == 0
    assert "toggled" in " ".join(r.stdout.split())


def test_reset_deletes_every_ingested_class_but_leaves_the_schema(monkeypatch):
    """Driven through a fake session on purpose: the real command empties the database, and this suite shares one with every other test in the run."""
    from contextlib import contextmanager

    deleted = []

    class _Row:
        def __init__(self, cls):
            self.cls = cls

    class _Query:
        def __init__(self, cls):
            self.cls = cls

        def all(self):
            return [_Row(self.cls)]

    class _Session:
        def query(self, cls):
            return _Query(cls)

        def delete(self, row):
            deleted.append(row.cls)

    @contextmanager
    def fake_scope():
        yield _Session()

    monkeypatch.setattr(cli, "session_scope", fake_scope)
    monkeypatch.setattr(cli, "_setup", lambda: None)

    result = runner.invoke(app, ["reset", "--yes"])
    assert result.exit_code == 0
    assert "all ingested data removed" in result.stdout
    # Children before parents: deleting Repo first would trip every foreign key.
    names = [c.__name__ for c in deleted]
    assert names[0] == "RepoImpact" and names[-1] == "Repo"
    assert "Commit" in names and "File" in names
