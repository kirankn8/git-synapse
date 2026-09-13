"""What a pasted URL means."""
from __future__ import annotations

import pytest

from git_synapse.ingest import sources
from git_synapse.ingest.sources import SourceError


@pytest.mark.parametrize("raw,provider,owner,repo,api", [
    # The browser URL, which is what people actually paste.
    ("https://github.com/microsoft/vscode", "github", "microsoft", "vscode", True),
    ("https://github.com/microsoft", "github", "microsoft", None, True),
    ("https://gitlab.com/gitlab-org/gitlab", "gitlab", "gitlab-org", "gitlab", True),
    ("https://bitbucket.org/atlassian/aui", "bitbucket", "atlassian", "aui", True),
    # Clone URLs, both shapes.
    ("https://github.com/microsoft/vscode.git", "github", "microsoft", "vscode", True),
    ("git@github.com:microsoft/vscode.git", "github", "microsoft", "vscode", True),
    ("git@gitlab.com:gitlab-org/gitlab.git", "gitlab", "gitlab-org", "gitlab", True),
    # A deep link, trimmed back to the repository it is inside.
    ("https://github.com/microsoft/vscode/tree/main/src", "github", "microsoft", "vscode", True),
    ("https://github.com/microsoft/vscode/issues/42", "github", "microsoft", "vscode", True),
    ("https://bitbucket.org/team/repo/src/main/", "bitbucket", "team", "repo", True),
    ("https://gitlab.com/gitlab-org/gitlab/-/merge_requests/1", "gitlab",
     "gitlab-org", "gitlab", True),
    # Trailing slashes and whitespace, which a copy-paste brings along.
    ("  https://github.com/microsoft/  ", "github", "microsoft", None, True),
    # owner/repo, which is how people write it in prose.
    ("torvalds/linux", "github", "torvalds", "linux", True),
    ("https://git.internal.corp/team/svc.git", "git", "team", "svc", False),
])
def test_a_url_resolves_to_the_thing_it_names(raw, provider, owner, repo, api):
    s = sources.parse(raw)
    assert (s.provider, s.owner, s.repo, s.has_api) == (provider, owner, repo, api)
    assert s.is_repo is (repo is not None)


def test_a_gitlab_group_may_nest():
    """`gitlab-org/security/gitlab` is one project in a subgroup, not a project called `security` -- and the owner is everything above the last segment."""
    s = sources.parse("https://gitlab.com/gitlab-org/security/gitlab")
    assert (s.owner, s.repo, s.full_name) == (
        "gitlab-org/security", "gitlab", "gitlab-org/security/gitlab")


def test_the_clone_url_is_always_https():
    """The mirror is read-only and anonymous unless a token is configured; an ssh remote would need a key in the container nothing else here wants."""
    assert sources.parse("git@github.com:a/b.git").clone_url == "https://github.com/a/b.git"


@pytest.mark.parametrize("raw", ["", "   ", "not a url at all", "https://", "https://host"])
def test_what_cannot_be_resolved_is_refused_with_a_reason(raw):
    with pytest.raises(SourceError):
        sources.parse(raw)


def test_a_self_hosted_port_survives():
    s = sources.parse("https://git.corp:8443/team/svc")
    assert s.web_url == "https://git.corp:8443/team/svc"
