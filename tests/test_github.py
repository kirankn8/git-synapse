"""The GitHub client, exercised at its HTTP boundary.

A mocked transport is right here and nowhere else: this is the one place the
code genuinely talks to a remote service, and the failures worth pinning are
protocol-level -- pagination that stops early, a 200 that is not a list, an
unauthenticated request that returns fewer repositories than exist.
"""
from __future__ import annotations

import httpx
import pytest

from git_synapse.config import GitHubConfig
from git_synapse.ingest.github import GitHubClient, RepoRecord, select_repos


def _repo_payload(i: int, **over):
    payload = {
        "id": 1000 + i, "name": f"repo{i}", "full_name": f"acme/repo{i}",
        "owner": {"login": "acme"},
        # A real github.com clone URL, because the host is now load-bearing: a
        # token is embedded only when the URL's host is the record's host.
        "clone_url": f"https://github.com/acme/repo{i}.git",
        "ssh_url": f"git@github.com:acme/repo{i}.git", "default_branch": "main",
        "size": 100, "archived": False, "fork": False, "disabled": False,
        "private": False, "visibility": "public", "language": "Go",
        "stargazers_count": 0, "topics": [], "description": None,
        "pushed_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "created_at": "2020-01-01T00:00:00Z",
    }
    payload.update(over)
    return payload


def _client(handler, token="ghu_" + "t" * 36):
    cfg = GitHubConfig(token=token, token_file="")
    c = GitHubClient(cfg)
    c._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=cfg.api_url,
        headers=dict(c._client.headers),
    )
    return c


# --------------------------------------------------------------- auth header

def test_the_live_token_reaches_the_authorization_header(tmp_path):
    """An unauthenticated request returns HTTP 200 and only public repositories
    -- 59 of 272 -- which discovery then accepted as the whole org."""
    token_file = tmp_path / "tok"
    token_file.write_text("ghu_" + "f" * 36)
    cfg = GitHubConfig(token="", token_file=str(token_file))
    with GitHubClient(cfg) as c:
        assert c._client.headers["Authorization"] == "Bearer ghu_" + "f" * 36


def test_no_token_means_no_authorization_header():
    cfg = GitHubConfig(token="", token_file="")
    with GitHubClient(cfg) as c:
        assert "Authorization" not in c._client.headers


# ---------------------------------------------------------------- pagination

def test_pagination_walks_every_page():
    pages = {1: [_repo_payload(i) for i in range(100)],
             2: [_repo_payload(100 + i) for i in range(30)]}

    def handler(request):
        page = int(dict(request.url.params).get("page", 1))
        return httpx.Response(200, json=pages.get(page, []))

    with _client(handler) as c:
        assert len(c.list_org_repos("acme")) == 130


def test_a_short_first_page_ends_the_walk():
    calls = []

    def handler(request):
        calls.append(int(dict(request.url.params).get("page", 1)))
        return httpx.Response(200, json=[_repo_payload(0)])

    with _client(handler) as c:
        assert len(c.list_org_repos("acme")) == 1
    assert calls == [1], "a short page means the last page; do not keep asking"


def test_an_http_error_raises_rather_than_returning_a_partial_list(monkeypatch):
    """A truncated listing accepted as complete is how the corpus silently
    shrank to a fifth of itself."""
    # The client retries a 5xx with backoff, which is right in production and
    # pointless here; the assertion is about what it does once it gives up.
    monkeypatch.setattr("git_synapse.ingest.github.time.sleep", lambda _s: None)

    def handler(request):
        page = int(dict(request.url.params).get("page", 1))
        if page == 1:
            return httpx.Response(200, json=[_repo_payload(i) for i in range(100)])
        return httpx.Response(500, json={"message": "boom"})

    with _client(handler) as c, pytest.raises(RuntimeError, match="failed after"):
        c.list_org_repos("acme")


def test_a_non_list_body_does_not_become_repositories():
    def handler(request):
        return httpx.Response(200, json={"message": "Not Found"})

    with _client(handler) as c:
        assert c.list_org_repos("acme") == []


# -------------------------------------------------------------- record shape

def test_record_is_built_from_the_api_payload():
    r = RepoRecord.from_api(_repo_payload(1, size=4096, language="Python"))
    assert r.full_name == "acme/repo1"
    assert r.default_branch == "main"
    assert r.disk_usage_kb == 4096
    assert r.primary_language == "Python"


def test_authed_clone_url_embeds_the_token_and_leaves_no_trace_without_one():
    r = RepoRecord.from_api(_repo_payload(1))
    assert r.authed_clone_url("ghu_abc").startswith("https://x-access-token:ghu_abc@")
    assert r.authed_clone_url("") == r.clone_url


def test_a_token_is_only_embedded_on_the_host_that_issued_it():
    """Otherwise the deployment-wide GitHub token is handed to whatever server
    a self-hosted repository happens to live on."""
    import dataclasses

    gh = RepoRecord.from_api(_repo_payload(1))
    assert "ghu_abc@" in gh.authed_clone_url("ghu_abc")

    # Same provider, different host: a GitHub Enterprise clone URL under a
    # record whose token belongs to github.com.
    elsewhere = dataclasses.replace(gh, clone_url="https://ghe.corp/o/r.git")
    assert elsewhere.authed_clone_url("ghu_abc") == "https://ghe.corp/o/r.git"

    # A host with no API client gets no credential at all.
    plain = dataclasses.replace(gh, provider="git", host="git.corp",
                                clone_url="https://git.corp/o/r.git")
    assert plain.authed_clone_url("ghu_abc") == "https://git.corp/o/r.git"


def test_each_host_gets_the_clone_username_it_expects():
    """A token with the wrong username beside it is simply a 401."""
    import dataclasses

    gh = RepoRecord.from_api(_repo_payload(1))
    gl = dataclasses.replace(gh, provider="gitlab", host="gitlab.com",
                             clone_url="https://gitlab.com/o/r.git")
    bb = dataclasses.replace(gh, provider="bitbucket", host="bitbucket.org",
                             clone_url="https://bitbucket.org/o/r.git")
    assert gl.authed_clone_url("glpat_x").startswith("https://oauth2:glpat_x@")
    assert bb.authed_clone_url("bb_x").startswith("https://x-token-auth:bb_x@")


# ------------------------------------------------------------------ filters

def test_archived_and_forked_repositories_follow_configuration():
    """A fork whose parent is nowhere in the corpus duplicates nothing.

    The archived repository is dropped outright; the fork is not, because
    ``acme/upstream`` is neither in this listing nor already tracked.
    """
    records = [
        RepoRecord.from_api(_repo_payload(1)),
        RepoRecord.from_api(_repo_payload(2, archived=True)),
        RepoRecord.from_api(_repo_payload(3, fork=True,
                                          parent={"full_name": "acme/upstream"})),
    ]
    keep_all = select_repos(records, GitHubConfig(include_archived=True, include_forks=True))
    assert len(keep_all) == 3

    plain = select_repos(records, GitHubConfig(include_archived=False, include_forks=False))
    assert {r.name for r in plain} == {"repo1", "repo3"}


def test_a_fork_is_dropped_only_when_its_parent_is_also_here():
    """The duplication is the harm, so the parent's presence is the question.

    Storing a fork beside its parent puts the same commits in twice and lets
    one project's history be counted as two projects agreeing.
    """
    cfg = GitHubConfig(include_forks=False)

    # Parent arriving in the same listing: an org that owns a project and a
    # fork of it hands us both at once.
    same_listing = [
        RepoRecord.from_api(_repo_payload(1)),
        RepoRecord.from_api(_repo_payload(2, fork=True,
                                          parent={"full_name": "acme/repo1"})),
    ]
    assert [r.name for r in select_repos(same_listing, cfg)] == ["repo1"]

    # Parent already in the corpus, and not in this listing at all.
    fork_only = [RepoRecord.from_api(_repo_payload(2, fork=True,
                                                   parent={"full_name": "acme/repo1"}))]
    assert select_repos(fork_only, cfg, tracked=frozenset({"acme/repo1"})) == []

    # Same fork, nothing tracked: nothing is duplicated, so it stays.
    assert [r.name for r in select_repos(fork_only, cfg)] == ["repo2"]


def test_the_parent_is_fetched_per_repository_because_listings_omit_it():
    """GitHub's list endpoints return the minimal repository representation,
    which carries `fork` but not `parent`. One request per fork closes that."""
    asked = []

    def handler(request):
        asked.append(request.url.path)
        return httpx.Response(200, json=_repo_payload(
            3, fork=True, parent={"full_name": "upstream/project"}))

    with _client(handler) as c:
        assert c.fetch_parent("acme/repo3") == "upstream/project"
    assert asked == ["/repos/acme/repo3"]


def test_a_repository_with_no_parent_reports_an_empty_one():
    def handler(request):
        return httpx.Response(200, json=_repo_payload(1))

    with _client(handler) as c:
        assert c.fetch_parent("acme/repo1") == ""


def test_a_failed_parent_lookup_is_empty_rather_than_fatal():
    """The caller keeps a fork it cannot place, so a refusal here must not be
    an exception that aborts discovery of the whole organisation.

    404 is the realistic failure: a repository renamed between the listing and
    this request, or one the token may list but not read.
    """
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    with _client(handler) as c:
        assert c.fetch_parent("acme/repo3") == ""


def test_a_fork_whose_parent_could_not_be_established_is_kept():
    """An empty parent means "not established", never "no parent".

    GitHub answers `parent` only on a per-repository request, so a failed or
    skipped one leaves it blank. Discarding real history on a failed request
    would lose a codebase somebody works in, and say nothing about why.
    """
    unplaced = [RepoRecord.from_api(_repo_payload(9, fork=True))]
    assert unplaced[0].parent_full_name == ""
    kept = select_repos(unplaced, GitHubConfig(include_forks=False),
                        tracked=frozenset({"acme/repo1"}))
    assert [r.name for r in kept] == ["repo9"]


def test_an_explicit_allowlist_overrides_every_other_filter():
    records = [RepoRecord.from_api(_repo_payload(i)) for i in range(3)]
    cfg = GitHubConfig(only_repos=["repo2"], include_archived=False, include_forks=False)
    assert [r.name for r in select_repos(records, cfg)] == ["repo2"]


# ------------------------------------------------------- retry and limits

def test_a_transient_5xx_is_retried_and_then_succeeds(monkeypatch):
    monkeypatch.setattr("git_synapse.ingest.github.time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(502, json={"message": "bad gateway"})
        return httpx.Response(200, json=[_repo_payload(0)])

    with _client(handler) as c:
        assert len(c.list_org_repos("acme")) == 1
    assert attempts["n"] == 3, "it must retry rather than give up on the first 502"


def test_rate_limiting_waits_and_retries(monkeypatch):
    """A 403 with a reset header is a wait, not a failure."""
    slept = []
    monkeypatch.setattr("git_synapse.ingest.github.time.sleep", slept.append)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"},
                json={"message": "API rate limit exceeded"},
            )
        return httpx.Response(200, json=[_repo_payload(0)])

    with _client(handler) as c:
        assert len(c.list_org_repos("acme")) == 1
    assert slept, "a rate limit must be waited out, not hammered"


def test_a_client_error_that_is_not_a_rate_limit_is_not_retried(monkeypatch):
    """Retrying a 404 just multiplies the latency."""
    monkeypatch.setattr("git_synapse.ingest.github.time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(404, json={"message": "Not Found"})

    with _client(handler) as c, pytest.raises(httpx.HTTPStatusError, match="404"):
        c.list_org_repos("acme")
    assert attempts["n"] == 1, f"a 404 was retried {attempts['n']} times"


def test_a_connection_error_is_retried_then_surfaced(monkeypatch):
    monkeypatch.setattr("git_synapse.ingest.github.time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ConnectError("no route to host")

    with _client(handler) as c, pytest.raises(RuntimeError, match="failed after"):
        c.list_org_repos("acme")
    assert attempts["n"] > 1, "a connection error must be retried before giving up"


def test_rate_limit_endpoint_returns_the_budget():
    def handler(request):
        return httpx.Response(200, json={"rate": {"remaining": 4321, "limit": 5000}})

    with _client(handler) as c:
        assert c.rate_limit()["rate"]["remaining"] == 4321


def test_fetch_languages_degrades_to_empty_rather_than_failing(monkeypatch):
    """Languages are a nice-to-have; losing them must not fail an ingest."""
    monkeypatch.setattr("git_synapse.ingest.github.time.sleep", lambda _s: None)

    def handler(request):
        return httpx.Response(500, json={})

    with _client(handler) as c:
        assert c.fetch_languages("acme/x") == {}


# ------------------------------------------------------------- the repo filters

def _record(name, **flags):
    return RepoRecord(github_id=hash(name) % 10**6, owner="acme", name=name,
                      full_name=f"acme/{name}", clone_url="",
                      default_branch="main", **flags)


def test_a_disabled_repository_is_never_included():
    """GitHub disables a repository when it is over quota or under review; there
    is nothing to clone."""
    import dataclasses

    from git_synapse.config import get_config
    from git_synapse.ingest.github import select_repos

    cfg = dataclasses.replace(get_config().github, only_repos=(), skip_repos=(),
                              include_private=True, include_forks=True,
                              include_archived=True)
    records = [_record("plain"), _record("dead", is_disabled=True)]
    assert [r.name for r in select_repos(records, cfg=cfg)] == ["plain"]


def test_skip_repos_matches_a_bare_name_or_a_full_name():
    import dataclasses

    from git_synapse.config import get_config
    from git_synapse.ingest.github import select_repos

    base = dataclasses.replace(get_config().github, only_repos=())
    records = [_record("keep"), _record("byname"), _record("byfullname")]

    cfg = dataclasses.replace(
        base, skip_repos=("ByName", "acme/byfullname"))
    assert [r.name for r in select_repos(records, cfg=cfg)] == ["keep"]


def test_a_disabled_repository_is_never_selected():
    """A disabled repository cannot be cloned at all, so selecting it turns one
    upstream state into a run-long sequence of failures."""
    from git_synapse.config import GitHubConfig
    from git_synapse.ingest.github import RepoRecord, select_repos

    cfg = GitHubConfig(org="acme")
    live = RepoRecord(github_id=1, owner="acme", name="live", full_name="acme/live")
    dead = RepoRecord(github_id=2, owner="acme", name="dead", full_name="acme/dead",
                      is_disabled=True)
    kept = {r.name for r in select_repos([live, dead], cfg)}
    assert kept == {"live"}


def test_a_private_repository_is_excluded_unless_asked_for():
    """Cloning it needs a credential, so selecting it when private access was
    not requested turns one setting into a clone failure."""
    from git_synapse.config import GitHubConfig
    from git_synapse.ingest.github import RepoRecord, select_repos

    public = RepoRecord(github_id=1, owner="acme", name="open", full_name="acme/open")
    secret = RepoRecord(github_id=2, owner="acme", name="shut", full_name="acme/shut",
                        is_private=True)
    assert {r.name for r in select_repos([public, secret],
                                         GitHubConfig(org="acme", include_private=False))} == {"open"}
    assert {r.name for r in select_repos([public, secret],
                                         GitHubConfig(org="acme", include_private=True))} == {"open", "shut"}


def test_giving_up_reports_what_the_host_actually_said(monkeypatch):
    """`failed after 5 attempts: None` was the message a reader got for a rate
    limit -- last_error is only set by transport errors, so the status and the
    host's own sentence, the two useful things, were both dropped."""
    import httpx

    from git_synapse.ingest.github import GitHubClient

    client = GitHubClient(patient=True)
    monkeypatch.setattr(client._client, "get", lambda path, params=None: httpx.Response(
        403, json={"message": "API rate limit exceeded for 1.2.3.4"},
        request=httpx.Request("GET", "https://x")))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(RuntimeError) as exc:
        client._get("/orgs/microsoft/repos")
    assert "HTTP 403" in str(exc.value)
    assert "rate limit exceeded" in str(exc.value)
    client.close()


def test_a_server_error_is_reported_with_its_status_too(monkeypatch):
    import httpx

    from git_synapse.ingest.github import GitHubClient

    client = GitHubClient(patient=True)
    monkeypatch.setattr(client._client, "get", lambda path, params=None: httpx.Response(
        503, text="upstream unavailable", request=httpx.Request("GET", "https://x")))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        client._get("/x")
    client.close()
