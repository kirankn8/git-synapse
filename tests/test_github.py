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
        "owner": {"login": "acme"}, "clone_url": f"https://x/repo{i}.git",
        "ssh_url": f"git@x:repo{i}.git", "default_branch": "main",
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

    with _client(handler) as c, pytest.raises(Exception):
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


# ------------------------------------------------------------------ filters

def test_archived_and_forked_repositories_follow_configuration():
    records = [
        RepoRecord.from_api(_repo_payload(1)),
        RepoRecord.from_api(_repo_payload(2, archived=True)),
        RepoRecord.from_api(_repo_payload(3, fork=True)),
    ]
    keep_all = select_repos(records, GitHubConfig(include_archived=True, include_forks=True))
    assert len(keep_all) == 3

    plain = select_repos(records, GitHubConfig(include_archived=False, include_forks=False))
    names = {r.name for r in plain}
    assert names == {"repo1"}


def test_an_explicit_allowlist_overrides_every_other_filter():
    records = [RepoRecord.from_api(_repo_payload(i)) for i in range(3)]
    cfg = GitHubConfig(only_repos=["repo2"], include_archived=False, include_forks=False)
    assert [r.name for r in select_repos(records, cfg)] == ["repo2"]


def test_a_disabled_repository_is_never_selected():
    records = [RepoRecord.from_api(_repo_payload(1, disabled=True))]
    assert select_repos(records, GitHubConfig(include_archived=True, include_forks=True)) == []


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

    with _client(handler) as c, pytest.raises(Exception):
        c.list_org_repos("acme")
    assert attempts["n"] == 1, f"a 404 was retried {attempts['n']} times"


def test_a_connection_error_is_retried_then_surfaced(monkeypatch):
    monkeypatch.setattr("git_synapse.ingest.github.time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ConnectError("no route to host")

    with _client(handler) as c, pytest.raises(Exception):
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
