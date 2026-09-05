"""The small remaining branches: accessors, formatters and rarely-hit returns.

Individually trivial. Collectively they are the parts of the codebase nothing
has ever executed, which is the only category where a typo survives review.
"""
from __future__ import annotations

import numpy as np
import pytest

# ------------------------------------------------------------ contingency

def test_contingency_reports_its_shape_and_repr():
    from git_synapse.stats.contingency import Contingency

    t = Contingency.from_counts(
        n_ab=np.array([2.0, 3.0]), n_a=np.array([5.0, 6.0]),
        n_b=np.array([4.0, 7.0]), n_total=np.array([10.0, 10.0]),
    )
    assert t.shape == (2,)
    assert "Contingency" in repr(t) or repr(t)


def test_a_scalar_contingency_has_scalar_shape():
    from git_synapse.stats.contingency import Contingency

    t = Contingency.from_counts(n_ab=2.0, n_a=5.0, n_b=4.0, n_total=10.0)
    assert t.shape in ((), (1,))


# --------------------------------------------------------------- registry

def test_computing_a_named_subset_of_measures():
    from git_synapse.stats.contingency import Contingency
    from git_synapse.stats.registry import compute_all

    t = Contingency.from_counts(
        n_ab=np.array([4.0]), n_a=np.array([6.0]),
        n_b=np.array([5.0]), n_total=np.array([20.0]),
    )
    out = compute_all(t, ("jaccard", "ochiai"))
    assert set(out) == {"jaccard", "ochiai"}
    assert 0.0 <= float(out["jaccard"][0]) <= 1.0


# ----------------------------------------------------------------- config

@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("0", False), ("false", False), ("", False), ("maybe", False)],
)
def test_boolean_settings_accept_the_usual_spellings(monkeypatch, raw, expected):
    from git_synapse.config import _env_bool

    monkeypatch.setenv("PROBE_BOOL", raw)
    assert _env_bool("PROBE_BOOL", default=False) is expected


def test_a_list_setting_splits_and_trims(monkeypatch):
    from git_synapse.config import _env_list

    monkeypatch.setenv("PROBE_LIST", " a , b ,, c ")
    assert _env_list("PROBE_LIST", ()) == ("a", "b", "c")


def test_an_absent_list_setting_uses_its_default(monkeypatch):
    from git_synapse.config import _env_list

    monkeypatch.delenv("PROBE_LIST", raising=False)
    assert _env_list("PROBE_LIST", ("x",)) == ("x",)


def test_the_sqlalchemy_url_names_the_psycopg_driver():
    from git_synapse.config import get_config

    assert get_config().db.url.startswith("postgresql+psycopg://")


# --------------------------------------------------------------- validate


# ------------------------------------------- the "should never happen" arguments

@pytest.mark.parametrize("level", ["", "FILE", "repo", "module", None])
def test_scoring_refuses_a_level_it_does_not_know(level):
    """file and dir are the only two. A typo must not silently score the wrong
    table -- the tables have different key columns, so it would half-work."""
    from git_synapse.analysis.score import _level_sql

    with pytest.raises(ValueError, match="unknown level"):
        _level_sql(level)


def test_scoring_knows_both_levels_it_claims_to():
    from git_synapse.analysis.score import _level_sql

    assert _level_sql("file")[0] == "file_pair"
    assert _level_sql("dir")[0] == "dir_pair"


def test_a_timestamp_github_did_not_send_is_none_not_an_error():
    from git_synapse.ingest.github import _parse_ts

    assert _parse_ts(None) is None
    assert _parse_ts("") is None


def test_a_timestamp_github_sent_wrong_is_none_not_an_error():
    """One unparseable field must not lose the whole repository record."""
    from git_synapse.ingest.github import _parse_ts

    assert _parse_ts("not-a-timestamp") is None
    assert _parse_ts("2026-13-45T99:00:00Z") is None


def test_githubs_trailing_z_is_understood():
    from git_synapse.ingest.github import _parse_ts

    got = _parse_ts("2026-08-26T10:00:00Z")
    assert got is not None and got.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(("headers", "expected"), [
    ({"retry-after": "30"}, 30),
    # Capped: a server that asks for an hour still gets retried within five
    # minutes, because the run has other repositories to get to.
    ({"retry-after": "99999"}, 300),
    ({"retry-after": "soon"}, 60),
    ({}, 60),
])
def test_a_rate_limited_request_waits_as_long_as_it_is_told_within_reason(headers,
                                                                         expected):
    from git_synapse.ingest.github import GitHubClient

    class _Resp:
        pass

    resp = _Resp()
    resp.headers = headers
    assert GitHubClient._rate_limit_wait(resp) == expected


@pytest.mark.parametrize(("delta", "expected"), [(120, 121), (-10, 60), (5000, 60)])
def test_a_reset_timestamp_is_honoured_only_when_it_is_plausible(delta, expected):
    """A clock-skewed or absurd reset header would otherwise stall the run."""
    import time as _time

    from git_synapse.ingest.github import GitHubClient

    class _Resp:
        pass

    resp = _Resp()
    resp.headers = {"x-ratelimit-reset": str(int(_time.time()) + delta)}
    assert GitHubClient._rate_limit_wait(resp) == expected


def test_a_contingency_table_with_a_collapsed_marginal_is_degenerate():
    """A file present in every commit, or in none, collapses a marginal. Every
    measure divides by one somewhere, so that is not a score of zero -- it is no
    score at all."""
    import numpy as np

    from git_synapse.stats.contingency import Contingency

    table = Contingency.from_counts(
        n_ab=np.array([5, 5, 5, 10, 0]),
        n_a=np.array([10, 0, 10, 100, 0]),
        n_b=np.array([10, 10, 0, 10, 0]),
        n_total=np.array([100, 100, 100, 100, 0]),
    )
    # Healthy; n_a collapsed; n_b collapsed; n_a fills every commit; empty.
    assert list(table.is_degenerate()) == [False, True, True, True, True]


def test_an_empty_record_is_ignored_by_the_commit_builder():
    """git's stream contains empty records between commits."""
    from git_synapse.ingest.parser import _CommitAssembler

    assembler = _CommitAssembler()
    assembler.feed("")
    assert not assembler.raw and not assembler.numstat
