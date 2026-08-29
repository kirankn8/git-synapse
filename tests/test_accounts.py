"""The accounts that drive discovery.

These decide which repositories exist at all, so a fault here is not a wrong
number in one view -- it is a repository that silently never gets scanned, or
one that gets scanned when someone deliberately excluded it.
"""

from __future__ import annotations

import pytest

from git_synapse.db.engine import query_one
from git_synapse.ingest import accounts
from git_synapse.ingest.accounts import AccountError


@pytest.fixture()
def clean(scratch_db):
    """An empty account table for each test."""
    query_one("DELETE FROM account RETURNING 1")
    return scratch_db


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize(
    "login",
    ["", "  ", "-leading", "trailing-", "double--hyphen", "a" * 40, "has space", "has_underscore"],
)
def test_a_login_github_could_not_own_is_rejected(login):
    """Rejected at the boundary, not as a puzzling 404 an hour into discovery."""
    with pytest.raises(AccountError):
        accounts.validate_login(login)


@pytest.mark.parametrize("login", ["kubernetes", "a", "a-b-c", "Org123", "a" * 39])
def test_a_valid_login_is_accepted(login):
    assert accounts.validate_login(login) == login


def test_a_login_is_trimmed_not_rejected_for_stray_whitespace():
    assert accounts.validate_login("  kubernetes  ") == "kubernetes"


def test_an_unknown_kind_is_rejected():
    with pytest.raises(AccountError):
        accounts.validate_kind("team")


@pytest.mark.parametrize(("given", "want"), [("org", "org"), ("USER", "user"), ("", "org")])
def test_kind_is_normalised(given, want):
    assert accounts.validate_kind(given) == want


@pytest.mark.parametrize(
    ("given", "want"),
    [
        (None, []),
        ("", []),
        ("a,b", ["a", "b"]),
        ("  a , , b ", ["a", "b"]),
        (["a", " b ", ""], ["a", "b"]),
    ],
)
def test_repo_lists_accept_csv_or_json_and_drop_blanks(given, want):
    assert accounts._names(given) == want


# ---------------------------------------------------------------------- CRUD

def test_an_account_round_trips(clean):
    made = accounts.add_account("kubernetes", kind="org", include_forks=False, skip_repos="a,b")
    got = accounts.get_account(made["id"])
    assert got["login"] == "kubernetes"
    assert got["kind"] == "org"
    assert got["include_forks"] is False
    assert got["skip_repos"] == ["a", "b"]
    assert got["enabled"] is True


def test_adding_the_same_login_twice_is_refused(clean):
    accounts.add_account("kubernetes")
    with pytest.raises(AccountError):
        accounts.add_account("kubernetes")


def test_a_duplicate_differing_only_in_case_is_refused(clean):
    """GitHub logins are case-insensitive, so two rows would scan the same org twice."""
    accounts.add_account("Kubernetes")
    with pytest.raises(AccountError):
        accounts.add_account("kubernetes")


def test_find_by_login_ignores_case(clean):
    accounts.add_account("Kubernetes")
    assert accounts.find_by_login("KUBERNETES")["login"] == "Kubernetes"


def test_update_changes_only_what_was_passed(clean):
    made = accounts.add_account("kubernetes", include_forks=True, include_archived=True)
    updated = accounts.update_account(made["id"], include_forks=False)
    assert updated["include_forks"] is False
    assert updated["include_archived"] is True, "an unpassed field must not be reset"
    assert updated["login"] == "kubernetes"


def test_update_with_no_fields_returns_the_row_unchanged(clean):
    made = accounts.add_account("kubernetes")
    assert accounts.update_account(made["id"])["id"] == made["id"]


def test_renaming_onto_another_account_is_refused(clean):
    accounts.add_account("one")
    two = accounts.add_account("two")
    with pytest.raises(AccountError):
        accounts.update_account(two["id"], login="one")


def test_renaming_to_its_own_login_is_allowed(clean):
    made = accounts.add_account("one")
    assert accounts.update_account(made["id"], login="one")["login"] == "one"


def test_updating_a_missing_account_raises(clean):
    with pytest.raises(AccountError):
        accounts.update_account(999_999, enabled=False)


def test_removing_a_missing_account_reports_false(clean):
    assert accounts.remove_account(999_999) is False


def test_enabled_only_filters_the_listing(clean):
    on = accounts.add_account("on")
    off = accounts.add_account("off")
    accounts.update_account(off["id"], enabled=False)
    logins = [r["login"] for r in accounts.list_accounts(enabled_only=True)]
    assert logins == ["on"] and on["login"] in logins


def test_record_discovery_stamps_the_outcome(clean):
    made = accounts.add_account("kubernetes")
    accounts.record_discovery(made["id"], 42)
    got = accounts.get_account(made["id"])
    assert got["repo_count"] == 42
    assert got["last_discovered_at"] is not None
    assert got["last_discover_error"] is None


def test_record_discovery_keeps_the_error_for_the_ui(clean):
    made = accounts.add_account("kubernetes")
    accounts.record_discovery(made["id"], 0, "404 Not Found")
    assert accounts.get_account(made["id"])["last_discover_error"] == "404 Not Found"


# ------------------------------------------------------------- config mapping

def test_config_for_overrides_only_this_accounts_settings(clean):
    made = accounts.add_account("kubernetes", include_forks=False, only_repos="a,b")
    cfg = accounts.config_for(made)
    assert cfg.org == "kubernetes"
    assert cfg.include_forks is False
    assert cfg.only_repos == ("a", "b")
    # Untouched settings still come from the environment.
    assert cfg.api_url


def test_config_for_falls_back_to_the_global_api_url(clean):
    made = accounts.add_account("kubernetes")
    assert accounts.config_for(made).api_url


def test_a_per_account_api_url_wins(clean):
    made = accounts.add_account("kubernetes", api_url="https://ghe.internal/api/v3")
    assert accounts.config_for(made).api_url == "https://ghe.internal/api/v3"


def test_a_blank_api_url_is_stored_as_null_not_empty(clean):
    made = accounts.add_account("kubernetes", api_url="   ")
    assert made["api_url"] is None


# ------------------------------------------------------------------- seeding

def test_seeding_adopts_the_legacy_env_org_once(clean, monkeypatch):
    """Without this an existing deployment discovers nothing after upgrading."""
    from git_synapse.config import reset_config_cache

    monkeypatch.setenv("GITHUB_ORG", "legacy-org")
    reset_config_cache()
    try:
        seeded = accounts.seed_from_env()
        assert seeded is not None and seeded["login"] == "legacy-org"
        assert accounts.seed_from_env() is None, "seeding twice would duplicate the org"
    finally:
        reset_config_cache()


def test_seeding_does_nothing_when_accounts_already_exist(clean):
    accounts.add_account("kubernetes")
    assert accounts.seed_from_env() is None


def test_seeding_does_nothing_without_an_env_org(clean, monkeypatch):
    from git_synapse.config import reset_config_cache

    monkeypatch.setenv("GITHUB_ORG", "")
    reset_config_cache()
    try:
        assert accounts.seed_from_env() is None
    finally:
        reset_config_cache()


def test_seeding_survives_an_invalid_env_org(clean, monkeypatch):
    """A bad GITHUB_ORG must not make every discovery raise on startup."""
    from git_synapse.config import reset_config_cache

    monkeypatch.setenv("GITHUB_ORG", "not a login")
    reset_config_cache()
    try:
        assert accounts.seed_from_env() is None
    finally:
        reset_config_cache()


# ---------------------------------------------- owner-driven resolution

def test_owners_are_read_from_ingested_repos_not_configured_accounts(clean):
    """Removing an account must not reclassify what it already mined."""
    from git_synapse.ingest.github import RepoRecord

    with_owner = RepoRecord.from_api({
        "id": 987654, "name": "thing", "full_name": "acme-owner/thing",
        "owner": {"login": "acme-owner"},
    })
    from git_synapse.db.engine import connection
    from git_synapse.ingest.store import upsert_repo

    with connection() as conn:
        upsert_repo(with_owner, conn)
    from git_synapse.db.engine import query
    owners = {r["owner"].lower() for r in query("SELECT owner FROM repo")}
    assert "acme-owner" in owners
