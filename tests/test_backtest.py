"""The backtest, which is the one thing that judges the product rather than the corpus.

The failure that matters here is leakage: if a commit's own pairs inform its own
prediction, every measure looks excellent and the number is worthless. Most of
these tests exist to pin that down.
"""

from __future__ import annotations

import pytest

from git_synapse.analysis import backtest as bt


def replay(monkeypatch, commits, **kw):
    """Run the backtest over a synthetic history, without touching the database."""
    monkeypatch.setattr(bt, "_history", lambda repo_id: commits)
    return bt.run(**kw)


def flat(pairs, times):
    """`times` commits, each touching the same set of files."""
    return [list(pairs) for _ in range(times)]


# ------------------------------------------------------------------- leakage

def test_a_commit_cannot_inform_its_own_prediction(monkeypatch):
    """The whole point. Files that only ever co-occur in the commit being
    scored must be unpredictable, because at that moment nothing has taught us
    they belong together."""
    history = flat([1, 2], bt.WARMUP_COMMITS + 1)   # noise to get past warmup
    history.append([90, 91])                        # first and only sighting
    result = replay(monkeypatch, history, measures=("npmi",))

    scored = [s for s in result.scores if s.measure == "npmi"][0]
    # The 90/91 prompts contribute two predictions and must both miss.
    assert scored.found == 0 or scored.hit_prompts < scored.prompts


def test_a_pair_becomes_predictable_only_after_it_has_been_seen(monkeypatch):
    """Same two files, but now with prior evidence, must be found."""
    history = flat([1, 2], bt.WARMUP_COMMITS)
    history += flat([90, 91], 5)                    # teach the pair
    history.append([90, 91])                        # then ask
    result = replay(monkeypatch, history, measures=("npmi",))

    scored = [s for s in result.scores if s.measure == "npmi"][0]
    assert scored.hit_prompts > 0, "a pair seen five times must be predictable"


def test_warmup_commits_are_not_scored(monkeypatch):
    """Predicting from an empty history would only depress the averages."""
    result = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS - 1), measures=("npmi",))
    assert result.prompts == 0
    assert result.verdict.startswith("no prompts")


def test_sweeping_commits_are_not_used_as_prompts(monkeypatch):
    """A 500-file reformat has no 'the file you are editing'."""
    history = flat([1, 2], bt.WARMUP_COMMITS)
    history.append(list(range(500, 500 + bt.MAX_FILES_PER_PROMPT + 5)))
    result = replay(monkeypatch, history, measures=("npmi",))
    assert result.prompts == 0


def test_single_file_commits_produce_no_prompt(monkeypatch):
    history = flat([1, 2], bt.WARMUP_COMMITS) + [[7]]
    before = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS), measures=("npmi",)).prompts
    assert replay(monkeypatch, history, measures=("npmi",)).prompts == before


# ------------------------------------------------------------------ baseline

def test_the_baseline_ignores_coupling_entirely(monkeypatch):
    """It answers with the busiest files, which is what makes lift meaningful."""
    history = flat([1, 2], bt.WARMUP_COMMITS + 10)
    result = replay(monkeypatch, history, measures=("npmi",))
    assert result.baseline.measure == "popularity"
    assert result.baseline.hit_rate > 0, "files 1 and 2 are the busiest, so it should hit"


def test_lift_is_relative_to_the_baseline(monkeypatch):
    history = flat([1, 2], bt.WARMUP_COMMITS + 10)
    result = replay(monkeypatch, history, measures=("npmi",))
    s = result.scores[0]
    if result.baseline.hit_rate:
        assert s.lift == pytest.approx(s.hit_rate / result.baseline.hit_rate)


def test_a_measure_that_only_matches_the_baseline_earns_no_lift(monkeypatch):
    """Two files that always move together: coupling and popularity agree, so
    the statistics have added nothing and lift must not exceed 1."""
    result = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS + 30), measures=("npmi",))
    assert result.scores[0].lift <= 1.0 + 1e-9


# ---------------------------------------------------------------- honest reporting

def test_a_small_sample_is_reported_as_inconclusive(monkeypatch):
    result = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS + 5), measures=("npmi",))
    assert result.prompts < bt.MIN_PROMPTS_FOR_A_VERDICT
    assert not result.conclusive
    assert "indicative only" in result.verdict


def test_rare_item_bias_is_carried_through_to_the_result(monkeypatch):
    """A measure can top the table precisely because it is biased; say so."""
    history = flat([1, 2], bt.WARMUP_COMMITS + 5)
    result = replay(monkeypatch, history, measures=("association_strength", "npmi"))
    flagged = {s.measure: s.rare_item_bias for s in result.scores}
    assert flagged["association_strength"] is True
    assert flagged["npmi"] is False


@pytest.mark.parametrize(("hits", "n"), [(0, 0), (0, 10), (10, 10), (5, 10), (1, 3)])
def test_the_confidence_interval_stays_within_zero_and_one(hits, n):
    low, high = bt.wilson(hits, n)
    assert 0.0 <= low <= high <= 1.0


def test_the_interval_narrows_as_evidence_accumulates():
    """A wide interval on thin evidence is the honest answer, not a defect."""
    thin = bt.wilson(5, 10)
    thick = bt.wilson(500, 1000)
    assert (thin[1] - thin[0]) > (thick[1] - thick[0])


def test_no_history_is_reported_rather_than_dividing_by_zero(monkeypatch):
    result = replay(monkeypatch, [], measures=("npmi",))
    assert result.prompts == 0 and result.commits_seen == 0
    assert result.best is None or result.best.hit_rate == 0.0
    assert result.verdict


# ------------------------------------------------------------------ mechanics

def test_every_requested_measure_is_scored(monkeypatch):
    keys = ("npmi", "jaccard", "ochiai")
    result = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS + 5), measures=keys)
    assert {s.measure for s in result.scores} == set(keys)


def test_results_are_ordered_best_first(monkeypatch):
    history = flat([1, 2], bt.WARMUP_COMMITS) + flat([1, 2, 3], 10)
    result = replay(monkeypatch, history, measures=("npmi", "jaccard", "ochiai"))
    rates = [s.hit_rate for s in result.scores]
    assert rates == sorted(rates, reverse=True)


def test_an_unknown_measure_is_refused_with_the_valid_names(monkeypatch):
    with pytest.raises(KeyError, match="unknown measure"):
        replay(monkeypatch, flat([1, 2], 5), measures=("not_a_measure",))


def test_min_support_suppresses_thin_evidence(monkeypatch):
    """Seen together once is not evidence; the option must actually apply."""
    history = flat([1, 2], bt.WARMUP_COMMITS)
    history += [[90, 91]] * 2 + [[90, 91]]
    loose = replay(monkeypatch, history, measures=("npmi",), min_support=2)
    strict = replay(monkeypatch, history, measures=("npmi",), min_support=99)
    assert strict.scores[0].found <= loose.scores[0].found
    assert strict.scores[0].found == 0


def test_limit_truncates_the_replay(monkeypatch):
    history = flat([1, 2], bt.WARMUP_COMMITS + 40)
    assert replay(monkeypatch, history, measures=("npmi",), limit=5).commits_seen == 5


def test_k_bounds_how_many_suggestions_are_counted(monkeypatch):
    history = flat([1, 2], bt.WARMUP_COMMITS) + flat([1, 2, 3, 4, 5, 6], 12)
    small = replay(monkeypatch, history, measures=("npmi",), k=1)
    large = replay(monkeypatch, history, measures=("npmi",), k=5)
    assert small.scores[0].found <= large.scores[0].found
