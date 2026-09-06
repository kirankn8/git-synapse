"""Adding something by pasting its URL.

The behaviour worth pinning down is what a paste *costs* and what it commits
to. A repository URL must not enumerate its owner -- that is the whole reason
this exists, since `microsoft` is 8,296 repositories and someone asking for
`vscode` asked one question. And a lookup must write nothing, because it is how
a person finds out whether the thing is there at all.
"""
from __future__ import annotations

import httpx
import pytest

from git_synapse.ingest import accounts, providers
from git_synapse.ingest.accounts import AccountError
from git_synapse.ingest.github import RepoRecord


def _record(full_name, **over):
    owner, _, name = full_name.partition("/")
    fields = {"github_id": 1, "owner": owner, "name": name, "full_name": full_name,
              "clone_url": f"https://github.com/{full_name}.git"}
    fields.update(over)
    return RepoRecord(**fields)


class _Fake:
    """A provider that records what it was asked, so a test can assert the
    *absence* of a listing rather than only the presence of a result."""

    def __init__(self, one=None, many=None, listing=True, error=None):
        self.one, self.many, self.listing, self.error = one, many, listing, error
        self.calls = []

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def supports_listing(self): return self.listing

    def get_repo(self, owner, name):
        self.calls.append(("get_repo", f"{owner}/{name}"))
        if self.error:
            raise self.error
        return self.one or _record(f"{owner}/{name}")

    def list_repos(self, owner):
        self.calls.append(("list_repos", owner))
        if self.error:
            raise self.error
        return list(self.many or [])


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def _install(client):
        holder["client"] = client
        monkeypatch.setattr(providers, "for_source",
                            lambda src, patient=True, token="": client)
        return client
    return _install


@pytest.fixture
def clean():
    """Remove anything a test added, whatever it was called."""
    before = {a["id"] for a in accounts.list_accounts()}
    yield
    for a in accounts.list_accounts():
        if a["id"] not in before:
            accounts.remove_account(a["id"])


# ------------------------------------------------------------------ lookups

def test_a_repository_url_costs_one_request_and_never_lists_the_owner(db, fake, clean):
    client = fake(_Fake(one=_record("microsoft/vscode", primary_language="TypeScript",
                                    stargazers=190931)))
    found = accounts.resolve_url("https://github.com/microsoft/vscode")
    assert found["kind"] == "repo"
    assert [c[0] for c in client.calls] == ["get_repo"]
    assert found["repos"][0]["full_name"] == "microsoft/vscode"
    assert found["total"] == 1


def test_a_lookup_writes_nothing(db, fake, clean):
    fake(_Fake(one=_record("acme/thing")))
    before = len(accounts.list_accounts())
    accounts.resolve_url("https://github.com/acme/thing")
    assert len(accounts.list_accounts()) == before


def test_an_owner_url_offers_the_repositories_under_it(db, fake, clean):
    fake(_Fake(many=[_record("acme/one", stargazers=10),
                     _record("acme/two", stargazers=99),
                     _record("acme/forked", is_fork=True),
                     _record("acme/old", is_archived=True)]))
    found = accounts.resolve_url("https://github.com/acme")
    assert found["kind"] == "owner" and found["total"] == 4
    # Most-starred first: a list somebody reads should start with what they
    # most likely came for.
    assert [r["name"] for r in found["repos"]][:2] == ["two", "one"]
    # Offered, but not pre-selected: a fork's history is its parent's, and an
    # archive cannot change again.
    suggested = {r["name"] for r in found["repos"] if r["suggested"]}
    assert suggested == {"one", "two"}


def test_a_host_with_no_api_says_what_to_do_instead(db, fake, clean):
    fake(_Fake(listing=False))
    with pytest.raises(AccountError, match="single repository"):
        accounts.resolve_url("https://git.corp/team")


def test_a_rate_limited_owner_still_offers_the_whole_owner(db, fake, clean):
    """Not a dead end: choosing from a list is one way to answer, and taking
    everything is the other -- and that one needs no list."""
    limit = httpx.HTTPStatusError(
        "429", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(429, request=httpx.Request("GET", "https://x")))
    fake(_Fake(error=limit))
    found = accounts.resolve_url("https://github.com/microsoft")
    assert found["kind"] == "owner" and found["repos"] == [] and found["total"] is None
    assert "listing_error" in found


def test_a_rate_limit_names_the_missing_token_when_there_is_none(db, fake, clean, monkeypatch):
    from git_synapse.config import get_config

    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", "")
    get_config.cache_clear()
    try:
        limit = httpx.HTTPStatusError(
            "403", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(403, request=httpx.Request("GET", "https://x")))
        fake(_Fake(error=limit))
        found = accounts.resolve_url("https://github.com/microsoft")
        assert "GITHUB_TOKEN" in found["listing_error"]
    finally:
        get_config.cache_clear()


def test_a_missing_repository_admits_it_might_be_private(db, fake, clean, monkeypatch):
    """A host answers 404 for both, deliberately. We cannot tell, and must not
    guess -- so both possibilities are stated, with the thing that separates
    them."""
    from git_synapse.config import get_config

    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", "")
    get_config.cache_clear()
    try:
        missing = httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(404, request=httpx.Request("GET", "https://x")))
        fake(_Fake(error=missing))
        with pytest.raises(AccountError) as exc:
            accounts.resolve_url("https://github.com/acme/secret")
        assert "private" in str(exc.value) and "GITHUB_TOKEN" in str(exc.value)
    finally:
        get_config.cache_clear()


def test_an_error_that_is_neither_is_raised_as_it_is(db, fake, clean):
    fake(_Fake(error=RuntimeError("the network fell over")))
    with pytest.raises(RuntimeError, match="fell over"):
        accounts.resolve_url("https://github.com/acme/thing")


def test_a_capped_listing_does_not_report_the_cap_as_the_total(db, fake, clean):
    """`total` is what the owner has. Stating the cap would be stating our own
    limit as a fact about somebody else's organisation."""
    fake(_Fake(many=[_record(f"acme/r{i}") for i in range(providers.LIST_CAP)]))
    found = accounts.resolve_url("https://github.com/acme")
    assert found["total"] is None and found["truncated"] is True
    assert found["shown"] == providers.LIST_CAP


# ------------------------------------------------------------------ writing

def test_adding_a_repository_tracks_only_it(db, fake, clean):
    fake(_Fake(one=_record("microsoft/vscode")))
    row = accounts.add_from_url("https://github.com/microsoft/vscode")
    assert row["login"] == "microsoft" and row["only_repos"] == ["vscode"]
    assert row["kind"] == "repo" and row["include_forks"] is False


def test_a_second_repository_from_the_same_owner_extends_the_list(db, fake, clean):
    fake(_Fake())
    accounts.add_from_url("https://github.com/microsoft/vscode")
    row = accounts.add_from_url("https://github.com/microsoft/TypeScript")
    assert row["only_repos"] == ["vscode", "TypeScript"]


def test_adding_the_same_repository_twice_changes_nothing(db, fake, clean):
    fake(_Fake())
    accounts.add_from_url("https://github.com/microsoft/vscode")
    row = accounts.add_from_url("https://github.com/microsoft/vscode")
    assert row["only_repos"] == ["vscode"]


def test_choosing_nothing_means_the_whole_owner(db, fake, clean):
    """The one intent an allowlist cannot express: everything, including what
    is created tomorrow."""
    fake(_Fake())
    row = accounts.add_from_url("https://github.com/acme", repos=[])
    assert row["only_repos"] == [] and row["kind"] == "org"


def test_naming_a_subset_of_an_owner_already_tracked_whole_does_not_narrow_it(db, fake, clean):
    fake(_Fake())
    accounts.add_from_url("https://github.com/acme", repos=[])
    row = accounts.add_from_url("https://github.com/acme/one")
    assert row["only_repos"] == [], "tracking everything must not collapse to one"


def test_a_gitlab_subgroup_project_is_keyed_by_its_path_under_the_owner(db, fake, clean):
    """Two projects in one group can share a name. Keying on the name alone
    puts one entry in the allowlist that then matches both."""
    fake(_Fake(many=[
        _record("gitlab-org/gitlab-runner", provider="gitlab", host="gitlab.com"),
        RepoRecord(github_id=None, owner="gitlab-org/ci-cd", name="gitlab-runner",
                   full_name="gitlab-org/ci-cd/gitlab-runner",
                   provider="gitlab", host="gitlab.com"),
    ]))
    found = accounts.resolve_url("https://gitlab.com/gitlab-org")
    assert sorted(r["key"] for r in found["repos"]) == [
        "ci-cd/gitlab-runner", "gitlab-runner"]


def test_a_source_on_another_host_is_a_different_source(db, fake, clean):
    """`acme` on github.com and `acme` on an internal GitLab are two places."""
    fake(_Fake())
    a = accounts.add_from_url("https://github.com/acme/thing")
    b = accounts.add_from_url("https://gitlab.com/acme/thing")
    assert a["id"] != b["id"]
    assert (a["host"], b["host"]) == ("github.com", "gitlab.com")


# ---------------------------------------------------------------- credentials

def test_a_token_given_while_adding_is_stored_encrypted(db, fake, clean, monkeypatch):
    from git_synapse import vault
    from git_synapse.db.engine import query_one

    monkeypatch.setenv(vault.ENV_KEY, "a-passphrase")
    fake(_Fake())
    row = accounts.add_from_url("https://github.com/acme/thing",
                                token="ghp_supersecrettokenvalue")
    assert row["has_credential"] is True
    assert "supersecret" not in str(row), "the plaintext must not come back out"
    raw = query_one("SELECT credential FROM account WHERE id = %s", (row["id"],))
    assert "supersecret" not in raw["credential"]
    assert accounts.credential_for(raw) == "ghp_supersecrettokenvalue"


def test_a_token_can_be_replaced_and_removed(db, fake, clean, monkeypatch):
    from git_synapse import vault

    monkeypatch.setenv(vault.ENV_KEY, "a-passphrase")
    fake(_Fake())
    row = accounts.add_from_url("https://github.com/acme/thing", token="ghp_" + "a" * 20)
    accounts.set_credential(row["id"], "ghp_" + "b" * 20)
    assert accounts.get_account(row["id"])["credential_hint"].endswith("bbbb")
    accounts.set_credential(row["id"], None)
    assert accounts.get_account(row["id"])["has_credential"] is False


def test_credential_for_falls_back_to_nothing(db):
    assert accounts.credential_for(None) == ""
    assert accounts.credential_for({}) == ""


# ------------------------------------------------- the allowlist fetch path

def test_an_allowlisted_source_fetches_by_name_and_never_lists(db, fake, clean):
    """The whole reason a repository URL is cheap: a source with an allowlist
    costs one request per named repository, every night, forever -- rather than
    the eighty-three pages it takes to enumerate an organisation of 8,296."""
    from git_synapse.ingest import pipeline

    client = fake(_Fake())
    row = accounts.add_from_url("https://github.com/acme/one")
    accounts.add_from_url("https://github.com/acme/two")
    records, raw = pipeline._discover_account(accounts.get_account(row["id"]))
    assert [c for c in client.calls if c[0] == "list_repos"] == []
    assert sorted(r.full_name for r in records) == ["acme/one", "acme/two"]
    assert raw == 2


def test_one_unfetchable_name_does_not_cost_the_others(db, fake, clean):
    """A repository that was renamed or deleted must not take its siblings
    down with it."""
    from git_synapse.ingest import pipeline

    class _Picky(_Fake):
        def get_repo(self, owner, name):
            if name == "gone":
                raise RuntimeError("404")
            return _record(f"{owner}/{name}")

    fake(_Picky())
    row = accounts.add_from_url("https://github.com/acme", repos=["one", "gone", "two"])
    records, _ = pipeline._discover_account(accounts.get_account(row["id"]))
    assert sorted(r.full_name for r in records) == ["acme/one", "acme/two"]


def test_an_allowlist_is_not_run_through_the_filters(db, fake, clean):
    """Somebody who named a fork wants that fork. A filter applied on top could
    only contradict them."""
    from git_synapse.ingest import pipeline

    fake(_Fake(one=_record("acme/a-fork", is_fork=True)))
    row = accounts.add_from_url("https://github.com/acme/a-fork")
    records, _ = pipeline._discover_account(accounts.get_account(row["id"]))
    assert [r.full_name for r in records] == ["acme/a-fork"]


def test_a_whole_owner_on_a_host_with_no_api_yields_nothing_rather_than_raising(
        db, fake, clean, caplog):
    """It cannot be enumerated, and a nightly discovery must not fail over it."""
    from git_synapse.ingest import pipeline

    fake(_Fake(listing=False))
    row = accounts.add_account("selfhosted", kind="org", provider="git", host="git.corp")
    records, raw = pipeline._discover_account(accounts.get_account(row["id"]))
    assert (records, raw) == ([], 0)


def test_the_gitlab_rate_limit_message_names_its_own_variable(db, fake, clean, monkeypatch):
    from git_synapse.config import get_config

    monkeypatch.setenv("GITLAB_TOKEN", "")
    get_config.cache_clear()
    try:
        limit = httpx.HTTPStatusError(
            "429", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(429, request=httpx.Request("GET", "https://x")))
        fake(_Fake(error=limit))
        found = accounts.resolve_url("https://gitlab.com/gitlab-org")
        assert "GITLAB_TOKEN" in found["listing_error"]
    finally:
        get_config.cache_clear()


def test_a_rate_limit_with_a_token_configured_blames_the_size_not_the_token(
        db, fake, clean, monkeypatch):
    from git_synapse.config import get_config

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "t" * 36)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", "")
    get_config.cache_clear()
    try:
        limit = httpx.HTTPStatusError(
            "429", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(429, request=httpx.Request("GET", "https://x")))
        fake(_Fake(error=limit))
        found = accounts.resolve_url("https://github.com/microsoft")
        assert "GITHUB_TOKEN" not in found["listing_error"]
        assert "rate-limiting" in found["listing_error"]
    finally:
        get_config.cache_clear()


def test_a_missing_repository_with_a_token_says_to_check_the_spelling(
        db, fake, clean, monkeypatch):
    from git_synapse.config import get_config

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "t" * 36)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", "")
    get_config.cache_clear()
    try:
        missing = httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(404, request=httpx.Request("GET", "https://x")))
        fake(_Fake(error=missing))
        with pytest.raises(AccountError, match="spelling"):
            accounts.resolve_url("https://github.com/acme/nope")
    finally:
        get_config.cache_clear()


def test_a_nested_gitlab_group_is_a_valid_login(db):
    """`gitlab-org/security` is one owner, and each segment is checked."""
    assert accounts.validate_login("gitlab-org/security") == "gitlab-org/security"
    with pytest.raises(AccountError):
        accounts.validate_login("gitlab-org/not a login")


def test_a_token_added_to_an_existing_source_is_stored(db, fake, clean, monkeypatch):
    from git_synapse import vault

    monkeypatch.setenv(vault.ENV_KEY, "a-passphrase")
    fake(_Fake())
    accounts.add_from_url("https://github.com/acme/one")
    row = accounts.add_from_url("https://github.com/acme/two", token="ghp_" + "z" * 20)
    assert row["has_credential"] is True and row["only_repos"] == ["one", "two"]


def test_a_clone_reads_the_source_credential(db, fake, clean, monkeypatch):
    from git_synapse import vault
    from git_synapse.ingest import pipeline

    monkeypatch.setenv(vault.ENV_KEY, "a-passphrase")
    fake(_Fake())
    accounts.add_from_url("https://github.com/acme/one", token="ghp_" + "c" * 20)
    assert pipeline._clone_token(_record("acme/one")) == "ghp_" + "c" * 20
    # A repository from a source that has none falls back to the environment.
    assert pipeline._clone_token(_record("nobody/thing")) == ""


def test_an_unreadable_credential_does_not_stop_an_ingest(db, monkeypatch):
    """The deployment-wide token is a working fallback; an exception here would
    take the whole run down over one source."""
    from git_synapse.ingest import pipeline

    monkeypatch.setattr(accounts, "find_by_login",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert pipeline._clone_token(_record("acme/one")) == ""


def test_a_rate_limited_single_repository_explains_rather_than_raising_httpx(
        db, fake, clean):
    """A repository lookup is one request, so hitting a limit here means the
    budget was already spent -- and the reader needs the same explanation."""
    limit = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(403, request=httpx.Request("GET", "https://x")))
    fake(_Fake(error=limit))
    with pytest.raises(AccountError, match=r"rate-limit|GITHUB_TOKEN"):
        accounts.resolve_url("https://github.com/acme/thing")


def test_a_listing_error_that_is_not_a_rate_limit_is_raised_as_it_is(db, fake, clean):
    """Only a rate limit has the "take the whole owner instead" answer. A
    genuine failure must not be dressed up as one."""
    fake(_Fake(error=RuntimeError("dns exploded")))
    with pytest.raises(RuntimeError, match="dns exploded"):
        accounts.resolve_url("https://github.com/acme")
