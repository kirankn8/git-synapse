"""The accounts that drive discovery."""

from __future__ import annotations

import pytest

from git_synapse.db.orm import models, session_scope
from git_synapse.ingest import accounts
from git_synapse.ingest.accounts import AccountError


@pytest.fixture()
def clean(scratch_db):
    """An empty account table for each test."""
    with session_scope() as session:
        session.query(models().Account).delete(synchronize_session=False)
    return scratch_db



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



def test_config_for_carries_this_accounts_settings_and_no_others(clean):
    """Two accounts scanned in one run must not see each other's filters, so every value comes from the row rather than from a shared default that the previous account might have replaced."""
    strict = accounts.add_account("kubernetes", include_forks=False, only_repos="a,b")
    loose = accounts.add_account("grafana", include_forks=True, include_archived=False)

    a, b = accounts.config_for(strict), accounts.config_for(loose)
    assert (a.include_forks, a.only_repos) == (False, ("a", "b"))
    assert (b.include_forks, b.include_archived) == (True, False)
    # Not named on either row, so both keep the default rather than the other's.
    assert a.include_archived is True
    assert b.only_repos == ()


def test_an_account_with_no_endpoint_of_its_own_stores_none(clean):
    """NULL means "the provider's public API", which `sources.parse` supplies."""
    made = accounts.add_account("kubernetes")
    assert made["api_url"] is None


def test_a_per_account_api_url_is_stored_as_given(clean):
    """A self-hosted install knows its endpoint; the host name cannot tell us."""
    made = accounts.add_account("kubernetes", api_url="https://ghe.internal/api/v3")
    assert made["api_url"] == "https://ghe.internal/api/v3"


def test_config_for_answers_only_what_is_taken(clean):
    """It returns a SelectionConfig: the same questions on every host."""
    from git_synapse.config import SelectionConfig

    made = accounts.add_account("kubernetes", include_forks=True, skip_repos="a,b")
    cfg = accounts.config_for(made)
    assert isinstance(cfg, SelectionConfig)
    assert cfg.include_forks is True
    assert cfg.skip_repos == ("a", "b")
    assert not hasattr(cfg, "api_url")


def test_a_blank_api_url_is_stored_as_null_not_empty(clean):
    made = accounts.add_account("kubernetes", api_url="   ")
    assert made["api_url"] is None



def test_owners_are_read_from_ingested_repos_not_configured_accounts(clean):
    """Removing an account must not reclassify what it already mined."""
    from git_synapse.ingest.github import RepoRecord

    with_owner = RepoRecord.from_api({
        "id": 987654, "name": "thing", "full_name": "acme-owner/thing",
        "owner": {"login": "acme-owner"},
    })
    from git_synapse.db.orm import session_scope
    from git_synapse.ingest.store import upsert_repo

    with session_scope() as conn:
        upsert_repo(with_owner, conn)
    with session_scope() as session:
        owners = {r.owner.lower() for r in session.query(models().Repo).all()}
    assert "acme-owner" in owners



def test_changing_the_kind_is_validated_like_creation(db):
    """`kind` decides which API listing is used, so a bad value fails the run later rather than here unless it is checked on update too."""
    acct = accounts.add_account("kindly")
    try:
        with pytest.raises(accounts.AccountError):
            accounts.update_account(acct["id"], kind="neither")
        assert accounts.update_account(acct["id"], kind="user")["kind"] == "user"
    finally:
        accounts.remove_account(acct["id"])


def test_an_allowlist_is_normalised_on_update(db):
    """Whitespace and empty entries in a pasted list would otherwise become repository names that match nothing."""
    acct = accounts.add_account("listy")
    try:
        row = accounts.update_account(acct["id"], only_repos=" one , , two ")
        assert row["only_repos"] == ["one", "two"]
    finally:
        accounts.remove_account(acct["id"])


def test_a_blank_api_url_is_stored_as_absent(db):
    """An empty string would be used as a base URL and fail every request."""
    acct = accounts.add_account("blanky")
    try:
        assert accounts.update_account(acct["id"], api_url="   ")["api_url"] is None
    finally:
        accounts.remove_account(acct["id"])


def test_updating_nothing_still_reports_an_unknown_account(db):
    """A no-op update on an id that does not exist must not look like success."""
    with pytest.raises(accounts.AccountError, match="not found"):
        accounts.update_account(-1)


def test_a_failed_discovery_does_not_zero_the_repository_count(db):
    """The listing did not come back."""
    from git_synapse.ingest import accounts

    src = accounts.add_account("countkeep", kind="org", provider="github",
                               host="github.com")
    try:
        accounts.record_discovery(src["id"], 122)
        assert accounts.get_account(src["id"])["repo_count"] == 122

        accounts.record_discovery(src["id"], error="403 rate limit exceeded")
        row = accounts.get_account(src["id"])
        assert row["repo_count"] == 122, "a failure must not rewrite the count"
        assert "rate limit" in row["last_discover_error"]

        # A successful pass does set it, including down.
        accounts.record_discovery(src["id"], 3)
        row = accounts.get_account(src["id"])
        assert row["repo_count"] == 3 and row["last_discover_error"] is None
    finally:
        accounts.remove_account(src["id"])


def test_the_count_is_reconciled_against_the_repositories_that_exist(db):
    """Discovery records what it *selected*, which is written before the upsert and therefore before anything is durable."""
    from git_synapse.ingest import accounts

    src = accounts.add_account("countreal", kind="org", provider="github",
                               host="github.com")
    try:
        # What discovery intended.
        accounts.record_discovery(src["id"], 7)
        # What actually landed.
        with session_scope() as session:
            session.add(models().Repo(owner="countreal", name="a", full_name="countreal/a",
                                      host="github.com", provider="github", account_id=src["id"],
                                      is_enabled=True))
        assert accounts.get_account(src["id"])["repo_count"] == 7

        accounts.refresh_repo_counts()
        assert accounts.get_account(src["id"])["repo_count"] == 1

        # A paused repository is not one a reader can click on.
        with session_scope() as session:
            row = session.query(models().Repo).filter_by(full_name="countreal/a").one()
            row.is_enabled = False
        accounts.refresh_repo_counts()
        assert accounts.get_account(src["id"])["repo_count"] == 0
        with session_scope() as session:
            assert session.query(models().Repo).filter_by(full_name="countreal/a").count() == 1
    finally:
        with session_scope() as session:
            row = session.query(models().Repo).filter_by(full_name="countreal/a").one_or_none()
            if row is not None:
                session.delete(row)
        accounts.remove_account(src["id"])


def test_updating_an_account_that_is_not_there_says_which_one(db):
    """The id came from a URL, so "not found" is the answer a 404 is built from -- not an empty success that looks like the edit was applied."""
    from git_synapse.ingest import accounts

    with pytest.raises(accounts.AccountError, match="999999999"):
        accounts.update_account(999_999_999, include_forks=True)


def test_recording_discovery_against_a_removed_account_is_a_no_op(db):
    """Discovery runs on a schedule and an account can be deleted while it is in flight."""
    from git_synapse.ingest import accounts

    accounts.record_discovery(999_999_999, error=None, repo_count=5)


def test_one_lookup_answers_what_credential_a_host_has(monkeypatch):
    """Every host's credential is reached the same way, so a caller asking "may we use anything against this host?" branches once rather than per vendor."""
    from git_synapse.config import Config, HostCredential, ProviderConfig

    cfg = Config(providers=ProviderConfig(
        github=HostCredential(token="ghp_" + "a" * 36, token_file=""),
        gitlab=HostCredential(token="glpat-token"),
        bitbucket=HostCredential(token="bb-token", user="someone")))

    assert cfg.providers.token_for("github") == "ghp_" + "a" * 36
    assert cfg.providers.token_for("gitlab") == "glpat-token"
    assert cfg.providers.token_for("bitbucket") == "bb-token"
    assert cfg.providers.token_for("gitea") == ""


def test_a_host_with_no_credential_reports_an_empty_one(monkeypatch):
    """Public repositories clone anonymously on every host, so "none set" is an ordinary answer rather than a misconfiguration."""
    from git_synapse.config import Config, HostCredential, ProviderConfig

    cfg = Config(providers=ProviderConfig(
        github=HostCredential(token="", token_file="")))
    assert cfg.providers.token_for("github") == ""
    assert cfg.providers.token_for("gitlab") == ""


def test_a_host_with_no_known_prefix_accepts_whatever_its_file_holds(tmp_path):
    """Only GitHub publishes what its credentials look like."""
    from git_synapse.config import HostCredential

    token_file = tmp_path / "gitlab-token"
    token_file.write_text("glpat-anything-at-all\n")

    host = HostCredential(token="fallback", token_file=str(token_file))
    assert host.current_token() == "glpat-anything-at-all"


def test_a_github_token_file_holding_something_else_is_ignored(tmp_path):
    """A half-written file would otherwise be sent as a credential and come back 401, which reads as "expired token" and costs somebody an afternoon."""
    from git_synapse.config import ProviderConfig

    token_file = tmp_path / "github-token"
    token_file.write_text("ghu_")          # a write caught mid-flight

    import dataclasses
    host = dataclasses.replace(ProviderConfig().github,
                               token="ghp_" + "w" * 36, token_file=str(token_file))
    # The working environment value survives; the malformed file does not win.
    assert host.current_token() == "ghp_" + "w" * 36
