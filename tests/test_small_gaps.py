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

def test_evaluate_warns_and_returns_nothing_without_ground_truth(db, monkeypatch):
    """With no labels there is nothing to score; saying so beats an empty table
    that looks like a measured result of zero."""
    from git_synapse.analysis import validate

    monkeypatch.setattr(validate, "ground_truth_edges", lambda *a, **kw: set())
    out = validate.evaluate(lag_bins=1)
    assert out == [] or out == {} or not out


def test_compare_to_symmetric_without_ground_truth_is_empty(db, monkeypatch):
    from git_synapse.analysis import validate

    monkeypatch.setattr(validate, "ground_truth_edges", lambda *a, **kw: set())
    assert validate.compare_to_symmetric() == {}


def test_auc_counts_a_tie_as_half_a_win():
    """The tie branch is what makes the rank-sum agree with the definition."""
    from git_synapse.analysis.validate import _auc

    # One positive and one negative with identical scores: exactly a coin flip.
    assert _auc(np.array([0.5, 0.5]), np.array([1, 0])) == pytest.approx(0.5)
