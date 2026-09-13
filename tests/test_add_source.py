"""Adding something by pasting its URL."""
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
    """A provider that records what it was asked, so a test can assert the *absence* of a listing rather than only the presence of a result."""

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

    def list_page(self, owner, page=1):
        self.calls.append(("list_page", owner, page))
        if self.error:
            raise self.error
        rows = list(self.many or [])
        start = (page - 1) * providers.PAGE
        chunk = rows[start:start + providers.PAGE]
        return providers.Page(chunk, has_more=len(rows) > start + providers.PAGE,
                              total=len(rows))


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



def test_a_repository_url_costs_one_request_and_never_lists_the_owner(db, fake, clean):
    client = fake(_Fake(one=_record("microsoft/vscode", primary_language="TypeScript",
                                    stargazers=190931)))
    found = accounts.resolve_url("https://github.com/microsoft/vscode")
    assert found["kind"] == "repo"
    assert [c[0] for c in client.calls] == ["get_repo"], "no listing at all"
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
    assert [r["name"] for r in found["repos"]][:2] == ["two", "one"]
    suggested = {r["name"] for r in found["repos"] if r["suggested"]}
    assert suggested == {"one", "two"}


def test_a_host_with_no_api_says_what_to_do_instead(db, fake, clean):
    fake(_Fake(listing=False))
    with pytest.raises(AccountError, match="single repository"):
        accounts.resolve_url("https://git.corp/team")


def test_a_rate_limited_owner_still_offers_the_whole_owner(db, fake, clean):
    """Not a dead end: choosing from a list is one way to answer, and taking everything is the other -- and that one needs no list."""
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
    """A host answers 404 for both, deliberately."""
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


def test_an_owner_arrives_one_page_at_a_time(db, fake, clean):
    """Every host caps a listing at a hundred, so 8,296 repositories is 83 requests -- which as one blocking call is twenty-five seconds of blank screen."""
    fake(_Fake(many=[_record(f"acme/r{i}") for i in range(250)]))
    first = accounts.resolve_url("https://github.com/acme")
    assert len(first["repos"]) == providers.PAGE
    assert first["page"] == 1 and first["has_more"] is True
    # The total is what the owner has, not what we have fetched so far.
    assert first["total"] == 250

    last = accounts.resolve_url("https://github.com/acme", page=3)
    assert len(last["repos"]) == 50 and last["has_more"] is False


def test_a_later_page_costs_one_request(db, fake, clean):
    """Page five must not be four pages of walking to reach it."""
    client = fake(_Fake(many=[_record(f"acme/r{i}") for i in range(500)]))
    accounts.resolve_url("https://github.com/acme", page=5)
    assert client.calls == [("list_page", "acme", 5)]


def test_each_page_is_cached_on_its_own(db, fake, clean):
    client = fake(_Fake(many=[_record(f"acme/r{i}") for i in range(250)]))
    accounts.resolve_url("https://github.com/acme", page=1)
    accounts.resolve_url("https://github.com/acme", page=2)
    assert accounts.resolve_url("https://github.com/acme", page=1)["cached"] is True
    assert accounts.resolve_url("https://github.com/acme", page=2)["cached"] is True
    assert len(client.calls) == 2



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
    """The one intent an allowlist cannot express: everything, including what is created tomorrow."""
    fake(_Fake())
    row = accounts.add_from_url("https://github.com/acme", repos=[])
    assert row["only_repos"] == [] and row["kind"] == "org"


def test_naming_a_subset_of_an_owner_already_tracked_whole_does_not_narrow_it(db, fake, clean):
    fake(_Fake())
    accounts.add_from_url("https://github.com/acme", repos=[])
    row = accounts.add_from_url("https://github.com/acme/one")
    assert row["only_repos"] == [], "tracking everything must not collapse to one"


def test_a_gitlab_subgroup_project_is_keyed_by_its_path_under_the_owner(db, fake, clean):
    """Two projects in one group can share a name."""
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



def test_a_token_given_while_adding_is_stored_encrypted(db, fake, clean, monkeypatch):
    from git_synapse import vault
    from git_synapse.db.orm import models, session_scope

    monkeypatch.setenv(vault.ENV_KEY, "a-passphrase")
    fake(_Fake())
    row = accounts.add_from_url("https://github.com/acme/thing",
                                token="ghp_supersecrettokenvalue")
    assert row["has_credential"] is True
    assert "supersecret" not in str(row), "the plaintext must not come back out"
    with session_scope() as session:
        stored = session.get(models().Account, row["id"])
        raw = {"credential": stored.credential}
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
    """The deployment-wide token is a working fallback; an exception here would take the whole run down over one source."""
    from git_synapse.ingest import pipeline

    monkeypatch.setattr(accounts, "find_by_login",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert pipeline._clone_token(_record("acme/one")) == ""


def test_a_rate_limited_single_repository_explains_rather_than_raising_httpx(
        db, fake, clean):
    """A repository lookup is one request, so hitting a limit here means the budget was already spent -- and the reader needs the same explanation."""
    limit = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(403, request=httpx.Request("GET", "https://x")))
    fake(_Fake(error=limit))
    with pytest.raises(AccountError, match=r"rate-limit|GITHUB_TOKEN"):
        accounts.resolve_url("https://github.com/acme/thing")


def test_a_listing_error_that_is_not_a_rate_limit_is_raised_as_it_is(db, fake, clean):
    """Only a rate limit has the "take the whole owner instead" answer."""
    fake(_Fake(error=RuntimeError("dns exploded")))
    with pytest.raises(RuntimeError, match="dns exploded"):
        accounts.resolve_url("https://github.com/acme")



@pytest.fixture(autouse=True)
def _empty_listing_cache():
    """Every test starts with a cold cache, or the second one to look up an owner asserts against the first one's answer."""
    accounts._LISTINGS.clear()
    yield
    accounts._LISTINGS.clear()


def test_a_second_look_at_the_same_owner_spends_nothing(db, fake, clean):
    """Paste, look, adjust, look again is one person's normal back-and-forth -- and on an anonymous GitHub it is three of the sixty requests in that hour."""
    client = fake(_Fake(many=[_record("acme/one")]))
    first = accounts.resolve_url("https://github.com/acme")
    second = accounts.resolve_url("https://github.com/acme")
    assert first["cached"] is False and second["cached"] is True
    assert [c[0] for c in client.calls] == ["list_page"], "asked the host once"
    assert second["repos"] == first["repos"]


def test_the_cache_expires(db, fake, clean, monkeypatch):
    fake(_Fake(many=[_record("acme/one")]))
    accounts.resolve_url("https://github.com/acme")
    now = accounts.time.time()
    monkeypatch.setattr(accounts.time, "time",
                        lambda: now + accounts.LISTING_TTL_SECONDS + 1)
    assert accounts.resolve_url("https://github.com/acme")["cached"] is False


def test_a_lookup_carrying_a_token_never_reads_the_anonymous_answer(db, fake, clean):
    """A token changes what is visible."""
    client = fake(_Fake(many=[_record("acme/one")]))
    accounts.resolve_url("https://github.com/acme")
    found = accounts.resolve_url("https://github.com/acme", token="ghp_x")
    assert found["cached"] is False
    assert len([c for c in client.calls if c[0] == "list_page"]) == 2


def test_a_refused_listing_is_not_cached(db, fake, clean):
    """Caching a failure would keep serving it after the budget refilled."""
    limit = httpx.HTTPStatusError(
        "429", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(429, request=httpx.Request("GET", "https://x")))
    fake(_Fake(error=limit))
    accounts.resolve_url("https://github.com/acme")
    assert accounts._LISTINGS == {}


def test_the_refusal_says_when_waiting_would_help(db, fake, clean, monkeypatch):
    """"Rate limited" leaves someone guessing between a minute and an hour."""
    from git_synapse.config import get_config
    from git_synapse.ingest import github

    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", "")
    get_config.cache_clear()

    class _Budget:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def rate_limit(self):
            import time as _t
            return {"resources": {"core": {"remaining": 0, "limit": 60,
                                           "reset": _t.time() + 12 * 60}}}

    monkeypatch.setattr(github, "GitHubClient", lambda *a, **k: _Budget())
    try:
        limit = httpx.HTTPStatusError(
            "403", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(403, request=httpx.Request("GET", "https://x")))
        fake(_Fake(error=limit))
        note = accounts.resolve_url("https://github.com/acme")["listing_error"]
        assert "0 of 60" in note and "12 minutes" in note
    finally:
        get_config.cache_clear()


def test_a_budget_that_cannot_be_read_is_simply_left_out(db, fake, clean, monkeypatch):
    """It is a nicety. Failing the whole lookup over it would be absurd."""
    from git_synapse.ingest import github

    monkeypatch.setattr(github, "GitHubClient",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    limit = httpx.HTTPStatusError(
        "429", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(429, request=httpx.Request("GET", "https://x")))
    fake(_Fake(error=limit))
    assert accounts.resolve_url("https://github.com/acme")["listing_error"]


def test_a_non_github_host_is_not_asked_for_a_github_budget(db, fake, clean):
    limit = httpx.HTTPStatusError(
        "429", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(429, request=httpx.Request("GET", "https://x")))
    fake(_Fake(error=limit))
    note = accounts.resolve_url("https://bitbucket.org/team")["listing_error"]
    assert "requests left" not in note


def test_a_small_owner_is_listed_once_however_many_names_are_given(db, fake, clean):
    """Seven names in an org of thirty is seven requests by name and one by listing."""
    from git_synapse.ingest import pipeline

    client = fake(_Fake(many=[_record(f"acme/r{i}") for i in range(30)]))
    names = [f"r{i}" for i in range(7)]
    row = accounts.add_from_url("https://github.com/acme", repos=names)
    records, _ = pipeline._discover_account(accounts.get_account(row["id"]))
    assert [c[0] for c in client.calls] == ["list_page"], "one request, not seven"
    assert sorted(r.name for r in records) == sorted(names)


def test_a_few_names_out_of_a_huge_owner_are_fetched_by_name(db, fake, clean):
    """One repository out of microsoft's 8,296 is one request by name and eighty-three by listing -- on every nightly refresh."""
    from git_synapse.ingest import pipeline

    client = fake(_Fake(many=[_record(f"big/r{i}") for i in range(1000)]))
    row = accounts.add_from_url("https://github.com/big", repos=["r5", "r7"])
    records, _ = pipeline._discover_account(accounts.get_account(row["id"]))
    kinds = [c[0] for c in client.calls]
    # One page to learn the size, then exactly the names asked for.
    assert kinds == ["list_page", "get_repo", "get_repo"]
    assert sorted(r.name for r in records) == ["r5", "r7"]


def test_many_names_out_of_a_large_owner_still_prefer_the_listing(db, fake, clean):
    """The comparison is pages against names, not a fixed number of either."""
    from git_synapse.ingest import pipeline

    client = fake(_Fake(many=[_record(f"big/r{i}") for i in range(300)]))
    names = [f"r{i}" for i in range(50)]
    row = accounts.add_from_url("https://github.com/big", repos=names)
    records, _ = pipeline._discover_account(accounts.get_account(row["id"]))
    # 2 pages left versus 50 names: page.
    assert {c[0] for c in client.calls} == {"list_page"}
    assert len(records) == 50


def test_one_unfetchable_name_does_not_cost_the_others_by_name(db, fake, clean):
    """A repository renamed or deleted upstream is a fact about that repository, not about the source it was named in."""
    from git_synapse.ingest import pipeline

    class _Picky(_Fake):
        def get_repo(self, owner, name):
            self.calls.append(("get_repo", f"{owner}/{name}"))
            if name == "gone":
                raise RuntimeError("404")
            return _record(f"{owner}/{name}")

    fake(_Picky(many=[_record(f"big/r{i}") for i in range(1000)]))
    row = accounts.add_from_url("https://github.com/big",
                                repos=["one", "gone", "two"])
    records, _ = pipeline._discover_account(accounts.get_account(row["id"]))
    assert sorted(r.name for r in records) == ["one", "two"]


def test_a_long_allowlist_on_a_host_that_cannot_list_falls_back_to_names(db, fake, clean):
    """There is no listing to be cheaper than."""
    from git_synapse.ingest import pipeline

    names = [f"r{i}" for i in range(30)]
    client = fake(_Fake(listing=False))
    row = accounts.add_account("selfhosted", kind="repo", provider="git",
                               host="git.corp", only_repos=names)
    records, _ = pipeline._discover_account(accounts.get_account(row["id"]))
    assert {c[0] for c in client.calls} == {"get_repo"}
    assert len(records) == len(names)


def test_an_ordinary_source_reaches_its_provider_not_the_no_api_fallback(db, clean, monkeypatch):
    """A NULL api_url on an account means "the provider's public API", but `has_api` reads None as "no API at all" -- so building the Source by hand here sent every ordinary GitHub source down the fallback and re-imported 164 repositories as bare git URLs with no stars, no fork flags and no visibility."""
    from git_synapse.ingest import pipeline, providers

    seen = {}

    def _spy(source, patient=True, token=""):
        seen["provider"] = source.provider
        seen["has_api"] = source.has_api
        raise RuntimeError("stop here; the Source is what is under test")

    monkeypatch.setattr(providers, "for_source", _spy)
    row = accounts.add_account("spy-org", kind="org", provider="github",
                               host="github.com")
    with pytest.raises(RuntimeError):
        pipeline._discover_account(accounts.get_account(row["id"]))
    assert seen == {"provider": "github", "has_api": True}


def test_a_self_hosted_source_keeps_its_own_endpoint(db, clean, monkeypatch):
    """The account knows the endpoint; the hostname cannot imply it."""
    from git_synapse.ingest import pipeline, providers

    seen = {}

    def _spy(source, patient=True, token=""):
        seen.update(provider=source.provider, api_url=source.api_url,
                    host=source.host, owner=source.owner)
        raise RuntimeError("stop")

    monkeypatch.setattr(providers, "for_source", _spy)
    row = accounts.add_account("platform", kind="group", provider="gitlab",
                               host="git.corp", api_url="https://git.corp/api/v4")
    with pytest.raises(RuntimeError):
        pipeline._discover_account(accounts.get_account(row["id"]))
    assert seen == {"provider": "gitlab", "api_url": "https://git.corp/api/v4",
                    "host": "git.corp", "owner": "platform"}


def test_a_host_with_no_client_still_gets_the_fallback(db, clean, monkeypatch):
    """The fallback must remain reachable -- it is the whole point of it."""
    from git_synapse.ingest import pipeline, providers

    seen = {}

    def _spy(source, patient=True, token=""):
        seen.update(provider=source.provider, has_api=source.has_api)
        raise RuntimeError("stop")

    monkeypatch.setattr(providers, "for_source", _spy)
    row = accounts.add_account("team", kind="org", provider="git", host="git.corp")
    with pytest.raises(RuntimeError):
        pipeline._discover_account(accounts.get_account(row["id"]))
    assert seen == {"provider": "git", "has_api": False}
