"""Asking a host what it has, and coping when it will not say."""
from __future__ import annotations

import httpx
import pytest

from git_synapse.ingest import providers, sources


def _resp(status: int, payload=None) -> httpx.Response:
    return httpx.Response(status, json=payload if payload is not None else {},
                          request=httpx.Request("GET", "https://example/x"))



@pytest.mark.parametrize("url,cls", [
    ("https://github.com/a/b", providers.GitHubProvider),
    ("https://gitlab.com/a/b", providers.GitLabProvider),
    ("https://bitbucket.org/a/b", providers.BitbucketProvider),
    ("https://git.corp/a/b", providers.GitProvider),
])
def test_each_host_gets_its_own_client(url, cls):
    assert isinstance(providers.for_source(sources.parse(url)), cls)


def test_a_host_with_no_api_still_produces_a_record():
    """The whole point of the fallback: coupling needs a clone, not an API."""
    source = sources.parse("https://git.corp/team/svc.git")
    with providers.for_source(source) as client:
        record = client.get_repo("team", "svc")
    assert record.full_name == "team/svc"
    assert record.clone_url == "https://git.corp/team/svc.git"
    assert record.host == "git.corp" and record.provider == "git"
    assert record.is_fork is False and record.github_id is None


def test_a_host_with_no_api_cannot_be_enumerated():
    """`supports_listing` is what stops the picker offering an empty list."""
    source = sources.parse("https://git.corp/team")
    assert providers.for_source(source).supports_listing() is False
    assert providers.for_source(sources.parse("https://github.com/a")).supports_listing()



def test_gitlab_projects_map_onto_the_record(monkeypatch):
    payload = {
        "path": "gitlab-runner", "path_with_namespace": "gitlab-org/gitlab-runner",
        "namespace": {"full_path": "gitlab-org"}, "description": "the runner",
        "web_url": "https://gitlab.com/gitlab-org/gitlab-runner",
        "http_url_to_repo": "https://gitlab.com/gitlab-org/gitlab-runner.git",
        "ssh_url_to_repo": "git@gitlab.com:gitlab-org/gitlab-runner.git",
        "default_branch": "main", "topics": ["ci"], "visibility": "public",
        "archived": True, "star_count": 2567, "forks_count": 12,
        "forked_from_project": {"id": 1},
    }
    source = sources.parse("https://gitlab.com/gitlab-org/gitlab-runner")
    client = providers.for_source(source)
    monkeypatch.setattr(client, "_get", lambda path, params=None: _resp(200, payload))
    r = client.get_repo("gitlab-org", "gitlab-runner")
    assert (r.full_name, r.provider, r.host) == (
        "gitlab-org/gitlab-runner", "gitlab", "gitlab.com")
    assert (r.stargazers, r.is_archived, r.is_fork, r.is_private) == (2567, True, True, False)


def test_bitbucket_repositories_map_onto_the_record(monkeypatch):
    payload = {
        "full_name": "atlassian/aui", "slug": "aui", "description": "d",
        "links": {"html": {"href": "https://bitbucket.org/atlassian/aui"},
                  "clone": [{"name": "ssh", "href": "git@..."},
                            {"name": "https", "href": "https://bitbucket.org/atlassian/aui.git"}]},
        "mainbranch": {"name": "master"}, "language": "java",
        "is_private": True, "size": 2048,
    }
    source = sources.parse("https://bitbucket.org/atlassian/aui")
    client = providers.for_source(source)
    monkeypatch.setattr(client._client, "get", lambda path: _resp(200, payload))
    r = client.get_repo("atlassian", "aui")
    assert (r.full_name, r.owner, r.name) == ("atlassian/aui", "atlassian", "aui")
    assert r.clone_url == "https://bitbucket.org/atlassian/aui.git"
    assert (r.is_private, r.visibility, r.primary_language) == (True, "private", "java")


def test_a_github_enterprise_record_carries_its_own_host(monkeypatch):
    """`from_api` knows GitHub's payload shape but not which GitHub."""
    source = sources.parse("https://ghe.corp/acme/thing")
    source = type(source)(**{**source.__dict__, "provider": "github",
                             "api_url": "https://ghe.corp/api/v3"})
    client = providers.for_source(source)
    payload = {"id": 7, "name": "thing", "full_name": "acme/thing",
               "owner": {"login": "acme"}, "clone_url": "https://ghe.corp/acme/thing.git"}
    monkeypatch.setattr(client._client, "_get", lambda path: _resp(200, payload))
    assert client.get_repo("acme", "thing").host == "ghe.corp"



def test_a_github_owner_that_is_not_an_org_is_tried_as_a_user(monkeypatch):
    """A person pasting a URL has no way to know which it is, and should not have to answer for it."""
    source = sources.parse("https://github.com/torvalds")
    client = providers.for_source(source)
    seen = []

    def _list(login, kind):
        seen.append(kind)
        if kind == "org":
            raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", "https://x"),
                                        response=_resp(404))
        return []

    monkeypatch.setattr(client._client, "list_account_repos", _list)
    assert client.list_repos("torvalds") == []
    assert seen == ["org", "user"]


def test_a_listing_failure_that_is_not_a_404_is_not_retried_as_a_user(monkeypatch):
    source = sources.parse("https://github.com/acme")
    client = providers.for_source(source)

    def _list(login, kind):
        raise httpx.HTTPStatusError("boom", request=httpx.Request("GET", "https://x"),
                                    response=_resp(500))

    monkeypatch.setattr(client._client, "list_account_repos", _list)
    with pytest.raises(httpx.HTTPStatusError):
        client.list_repos("acme")


def test_gitlab_falls_back_from_a_group_to_a_user(monkeypatch):
    source = sources.parse("https://gitlab.com/someone")
    client = providers.for_source(source)
    seen = []

    def _get(path, params=None):
        seen.append(path)
        if path.startswith("/groups"):
            raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", "https://x"),
                                        response=_resp(404))
        return _resp(200, [])

    monkeypatch.setattr(client, "_get", _get)
    assert client.list_repos("someone") == []
    assert seen[0].startswith("/groups") and seen[1].startswith("/users")


def test_bitbucket_follows_its_pagination_and_stops_at_the_cap(monkeypatch):
    source = sources.parse("https://bitbucket.org/atlassian")
    client = providers.for_source(source)
    page = {"values": [{"full_name": f"atlassian/r{i}", "links": {}} for i in range(100)],
            "next": "/repositories/atlassian?page=2"}

    monkeypatch.setattr(client._client, "get", lambda url: _resp(200, page))
    rows = client.list_repos("atlassian")
    # Endless `next`, so only the cap stops it -- which is the point of the cap.
    assert len(rows) >= providers.LIST_CAP


def test_gitlab_stops_at_the_cap_too(monkeypatch):
    source = sources.parse("https://gitlab.com/gitlab-org")
    client = providers.for_source(source)
    page = [{"path": f"r{i}", "path_with_namespace": f"gitlab-org/r{i}",
             "namespace": {"full_path": "gitlab-org"}} for i in range(100)]
    monkeypatch.setattr(client, "_get", lambda path, params=None: _resp(200, page))
    assert len(client.list_repos("gitlab-org")) >= providers.LIST_CAP


def test_gitlab_stops_when_a_page_comes_back_short(monkeypatch):
    source = sources.parse("https://gitlab.com/small")
    client = providers.for_source(source)
    monkeypatch.setattr(client, "_get", lambda path, params=None: _resp(
        200, [{"path": "one", "path_with_namespace": "small/one",
               "namespace": {"full_path": "small"}}]))
    assert len(client.list_repos("small")) == 1


def test_an_empty_first_page_ends_the_walk(monkeypatch):
    source = sources.parse("https://gitlab.com/empty")
    client = providers.for_source(source)
    monkeypatch.setattr(client, "_get", lambda path, params=None: _resp(200, []))
    assert client.list_repos("empty") == []



def test_a_source_token_overrides_the_environment_credential(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "from-the-environment")
    from git_synapse.config import get_config

    get_config.cache_clear()
    try:
        source = sources.parse("https://gitlab.com/a/b")
        client = providers.for_source(source, token="this-source-only")
        assert client._client.headers["PRIVATE-TOKEN"] == "this-source-only"
        assert providers.for_source(source)._client.headers["PRIVATE-TOKEN"] \
            == "from-the-environment"
    finally:
        get_config.cache_clear()


def test_a_github_source_token_beats_the_token_file(monkeypatch, tmp_path):
    """`current_token()` prefers the file, so a source credential that did not also clear it would be silently ignored."""
    from git_synapse.config import get_config

    path = tmp_path / "tok"
    path.write_text("ghu_" + "f" * 36)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(path))
    get_config.cache_clear()
    try:
        client = providers.for_source(sources.parse("https://github.com/a/b"),
                                      token="ghp_" + "s" * 36)
        assert client._client.cfg.current_token() == "ghp_" + "s" * 36
    finally:
        get_config.cache_clear()


def test_an_impatient_client_reports_a_rate_limit_instead_of_sleeping(monkeypatch):
    """A handler someone is watching has seconds. Sleeping sixty of them is indistinguishable from a hang."""
    from git_synapse.ingest.github import GitHubClient

    client = GitHubClient(patient=False)
    monkeypatch.setattr(client._client, "get", lambda path, params=None: _resp(403))
    monkeypatch.setattr("time.sleep", lambda *_: pytest.fail("it slept"))
    with pytest.raises(httpx.HTTPStatusError):
        client._get("/anything")
    client.close()


def test_the_base_provider_declares_what_a_client_must_answer():
    """A subclass that forgets one should fail loudly rather than return None."""
    from git_synapse.ingest.sources import Source

    base = providers.Provider(Source("git", "h", "o", None, None, "", ""))
    with pytest.raises(NotImplementedError):
        base.get_repo("o", "r")
    with pytest.raises(NotImplementedError):
        base.list_repos("o")
    assert base.close() is None


def test_every_client_closes_its_connection():
    """A leaked httpx client holds a socket open per lookup."""
    for url in ["https://github.com/a/b", "https://gitlab.com/a/b",
                "https://bitbucket.org/a/b"]:
        with providers.for_source(sources.parse(url)) as client:
            pass
        inner = getattr(client, "_client", None)
        inner = getattr(inner, "_client", inner)     # GitHubClient wraps one
        assert inner.is_closed


def test_bitbucket_uses_basic_auth_when_a_username_is_configured(monkeypatch):
    """An app password is basic auth, so it is useless without the username."""
    from git_synapse.config import get_config

    monkeypatch.setenv("BITBUCKET_USER", "someone")
    monkeypatch.setenv("BITBUCKET_TOKEN", "app-password")
    get_config.cache_clear()
    try:
        client = providers.for_source(sources.parse("https://bitbucket.org/a/b"))
        assert client._client.auth is not None
    finally:
        get_config.cache_clear()


def test_a_bitbucket_token_without_a_username_is_not_sent(monkeypatch):
    from git_synapse.config import get_config

    monkeypatch.setenv("BITBUCKET_USER", "")
    monkeypatch.setenv("BITBUCKET_TOKEN", "app-password")
    get_config.cache_clear()
    try:
        assert providers.for_source(sources.parse("https://bitbucket.org/a/b"))._client.auth is None
    finally:
        get_config.cache_clear()


def test_the_gitlab_get_helper_raises_on_an_error_status(monkeypatch):
    client = providers.for_source(sources.parse("https://gitlab.com/a/b"))
    monkeypatch.setattr(client._client, "get", lambda path, params=None: _resp(500))
    with pytest.raises(httpx.HTTPStatusError):
        client._get("/anything")


def test_a_gitlab_listing_failure_on_a_later_page_is_not_retried_as_a_user(monkeypatch):
    """The group/user fallback only makes sense for the very first request."""
    source = sources.parse("https://gitlab.com/g")
    client = providers.for_source(source)
    calls = []

    def _get(path, params=None):
        calls.append(params.get("page"))
        if params.get("page") == 2:
            raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", "https://x"),
                                        response=_resp(404))
        return _resp(200, [{"path": f"r{i}", "path_with_namespace": f"g/r{i}",
                            "namespace": {"full_path": "g"}} for i in range(100)])

    monkeypatch.setattr(client, "_get", _get)
    with pytest.raises(httpx.HTTPStatusError):
        client.list_repos("g")


def test_the_gitlab_get_helper_returns_the_response_on_success(monkeypatch):
    client = providers.for_source(sources.parse("https://gitlab.com/a/b"))
    monkeypatch.setattr(client._client, "get", lambda path, params=None: _resp(200, {"ok": 1}))
    assert client._get("/x").json() == {"ok": 1}



def _page_resp(rows, link="", headers=None):
    return httpx.Response(200, json=rows, headers={"link": link, **(headers or {})},
                          request=httpx.Request("GET", "https://example/x"))


def test_github_reads_the_page_it_was_asked_for_and_the_total_from_the_link(monkeypatch):
    """`rel="last"` is already in the response, so knowing an organisation has about 8,300 repositories costs nothing extra."""
    source = sources.parse("https://github.com/acme")
    client = providers.for_source(source)
    seen = {}

    def _get(path, params=None):
        seen.update({"path": path, **(params or {})})
        return _page_resp(
            [{"id": i, "name": f"r{i}", "full_name": f"acme/r{i}",
              "owner": {"login": "acme"}} for i in range(100)],
            link='<https://api.github.com/x?page=2>; rel="next", '
                 '<https://api.github.com/x?page=83>; rel="last"')

    monkeypatch.setattr(client._client, "_get", _get)
    page = client.list_page("acme", 3)
    assert seen["page"] == 3 and seen["per_page"] == providers.PAGE
    assert seen["path"] == "/orgs/acme/repos"
    assert len(page.records) == 100 and page.has_more is True
    assert page.total == 8300


def test_github_falls_back_to_the_user_endpoint_within_the_same_request(monkeypatch):
    """Probing first would be an extra request on every lookup, and anonymous GitHub allows sixty an hour."""
    client = providers.for_source(sources.parse("https://github.com/torvalds"))
    seen = []

    def _get(path, params=None):
        seen.append(path)
        if path.startswith("/orgs"):
            raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", "https://x"),
                                        response=_resp(404))
        return _page_resp([])

    monkeypatch.setattr(client._client, "_get", _get)
    client.list_page("torvalds", 1)
    assert seen == ["/orgs/torvalds/repos", "/users/torvalds/repos"]

    # And it is remembered, so page two does not repeat the 404.
    seen.clear()
    client.list_page("torvalds", 2)
    assert seen == ["/users/torvalds/repos"]


def test_a_github_error_that_is_not_a_404_is_not_retried_as_a_user(monkeypatch):
    client = providers.for_source(sources.parse("https://github.com/acme"))

    def _get(path, params=None):
        raise httpx.HTTPStatusError("boom", request=httpx.Request("GET", "https://x"),
                                    response=_resp(500))

    monkeypatch.setattr(client._client, "_get", _get)
    with pytest.raises(httpx.HTTPStatusError):
        client.list_page("acme", 1)


def test_a_last_page_reports_the_count_it_can_see(monkeypatch):
    """No `next` link means this is the end, so the total is knowable exactly."""
    client = providers.for_source(sources.parse("https://github.com/acme"))
    monkeypatch.setattr(client._client, "_get", lambda path, params=None: _page_resp(
        [{"id": i, "name": f"r{i}", "full_name": f"acme/r{i}", "owner": {"login": "acme"}}
         for i in range(17)]))
    page = client.list_page("acme", 1)
    assert page.has_more is False and page.total == 17


def test_a_page_with_a_next_but_no_last_link_reports_an_unknown_total(monkeypatch):
    """Unknown must stay unknown rather than becoming what we have so far."""
    client = providers.for_source(sources.parse("https://github.com/acme"))
    monkeypatch.setattr(client._client, "_get", lambda path, params=None: _page_resp(
        [{"id": i, "name": f"r{i}", "full_name": f"acme/r{i}", "owner": {"login": "acme"}}
         for i in range(100)],
        link='<https://api.github.com/x?page=2>; rel="next"'))
    assert client.list_page("acme", 1).total is None


def test_gitlab_reads_a_page_and_its_headers(monkeypatch):
    client = providers.for_source(sources.parse("https://gitlab.com/gitlab-org"))
    monkeypatch.setattr(client, "_get", lambda path, params=None: _page_resp(
        [{"path": f"r{i}", "path_with_namespace": f"gitlab-org/r{i}",
          "namespace": {"full_path": "gitlab-org"}} for i in range(100)],
        headers={"x-total": "3713", "x-next-page": "2"}))
    page = client.list_page("gitlab-org", 1)
    assert page.total == 3713 and page.has_more is True and len(page.records) == 100


def test_gitlab_without_a_total_header_says_unknown(monkeypatch):
    """GitLab drops the header once a set is large enough that counting costs."""
    client = providers.for_source(sources.parse("https://gitlab.com/g"))
    monkeypatch.setattr(client, "_get", lambda path, params=None: _page_resp(
        [{"path": "one", "path_with_namespace": "g/one", "namespace": {"full_path": "g"}}]))
    page = client.list_page("g", 1)
    assert page.total is None and page.has_more is False


def test_gitlab_page_falls_back_from_a_group_to_a_user(monkeypatch):
    client = providers.for_source(sources.parse("https://gitlab.com/someone"))
    seen = []

    def _get(path, params=None):
        seen.append(path)
        if path.startswith("/groups"):
            raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", "https://x"),
                                        response=_resp(404))
        return _page_resp([])

    monkeypatch.setattr(client, "_get", _get)
    client.list_page("someone", 1)
    assert seen[0].startswith("/groups") and seen[1].startswith("/users")


def test_a_gitlab_page_error_that_is_not_a_404_is_raised(monkeypatch):
    client = providers.for_source(sources.parse("https://gitlab.com/g"))

    def _get(path, params=None):
        raise httpx.HTTPStatusError("boom", request=httpx.Request("GET", "https://x"),
                                    response=_resp(500))

    monkeypatch.setattr(client, "_get", _get)
    with pytest.raises(httpx.HTTPStatusError):
        client.list_page("g", 1)


def test_bitbucket_pages_by_number_and_reads_its_size(monkeypatch):
    client = providers.for_source(sources.parse("https://bitbucket.org/atlassian"))
    seen = {}

    def _get(path, params=None):
        seen.update({"path": path, **(params or {})})
        return _page_resp({"values": [{"full_name": f"atlassian/r{i}", "links": {}}
                                      for i in range(100)],
                           "size": 407, "next": "..."})

    monkeypatch.setattr(client._client, "get", _get)
    page = client.list_page("atlassian", 2)
    assert seen["page"] == 2 and seen["pagelen"] == providers.PAGE
    assert page.total == 407 and page.has_more is True


def test_bitbucket_without_a_size_says_unknown(monkeypatch):
    client = providers.for_source(sources.parse("https://bitbucket.org/team"))
    monkeypatch.setattr(client._client, "get",
                        lambda path, params=None: _page_resp({"values": []}))
    page = client.list_page("team", 1)
    assert page.total is None and page.has_more is False


def test_a_client_that_cannot_page_is_sliced_out_of_a_full_listing():
    """The default keeps any future client correct without needing to know how that host paginates."""
    class _Simple(providers.Provider):
        def list_repos(self, owner):
            from git_synapse.ingest.github import RepoRecord
            return [RepoRecord(github_id=None, owner=owner, name=f"r{i}",
                               full_name=f"{owner}/r{i}") for i in range(150)]

    client = _Simple(sources.parse("https://git.corp/team"))
    first = client.list_page("team", 1)
    second = client.list_page("team", 2)
    assert len(first.records) == providers.PAGE and first.has_more is True
    assert len(second.records) == 50 and second.has_more is False
    assert first.total == 150


def test_a_host_that_names_the_parent_in_its_listing_asks_nothing_extra():
    """GitLab puts `forked_from_project` in the listing, so the base answers "" and no second request is made."""
    class _Simple(providers.Provider):
        def list_repos(self, owner):
            return []

    client = _Simple(sources.parse("https://git.corp/team"))
    assert client.fetch_parent("team/anything") == ""


def test_the_github_provider_asks_its_client_for_the_parent():
    """One request per fork, delegated rather than reimplemented, so Enterprise hosts and the api_url override are honoured the same way."""
    asked = []

    class _Client:
        def fetch_parent(self, full_name):
            asked.append(full_name)
            return "upstream/project"

    client = providers.GitHubProvider(sources.parse("https://github.com/acme"))
    client._client = _Client()
    assert client.fetch_parent("acme/fork") == "upstream/project"
    assert asked == ["acme/fork"]
