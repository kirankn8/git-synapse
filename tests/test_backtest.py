"""The backtest, which is the one thing that judges the product rather than the corpus.

The failure that matters here is leakage: if a commit's own pairs inform its own
prediction, every measure looks excellent and the number is worthless. Most of
these tests exist to pin that down.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from git_synapse.analysis import backtest as bt


def replay(monkeypatch, commits, paths=None, **kw):
    """Run the backtest over a synthetic history, without touching the database.

    The neighbour baselines need file paths, so a synthetic layout is supplied
    too: one directory per repository unless a test says otherwise.
    """
    monkeypatch.setattr(bt, "_history", lambda repo_id: [
        (repo, files, i) for i, (repo, files) in enumerate(commits)])
    if paths is None:
        ids = {f for _, files in commits for f in files}
        paths = {f: f"repo/dir/f{f}.py" for f in ids}
    by_stem, by_dir = {}, {}
    for fid, path in paths.items():
        by_stem.setdefault(bt.stem_of(path), []).append(fid)
        by_dir.setdefault(path.rsplit("/", 1)[0] if "/" in path else "", []).append(fid)
    monkeypatch.setattr(bt, "_path_index", lambda repo_id: (paths, by_stem, by_dir))
    return bt.run(**kw)


def flat(pairs, times, repo=1):
    """`times` commits in one repository, each touching the same files."""
    return [(repo, list(pairs)) for _ in range(times)]


def worktree(tmp_path, name):
    """A non-bare repository, since tags on branches need a working tree."""
    work = tmp_path / name
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True, env={**os.environ, **ENV})
    (work / "a.txt").write_text("x")
    g(work, "add", "-A")
    g(work, "commit", "--quiet", "-m", "one")
    return work


def g(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          env={**os.environ, **ENV}, capture_output=True, text=True)


def add_commit(repo, name, body):
    (repo / name).write_text(body)
    g(repo, "add", "-A")
    g(repo, "commit", "--quiet", "-m", name)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, env={**os.environ, **ENV})


ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


@pytest.fixture
def repo(tmp_path):
    """A real repository, because the search baseline is defined by what git
    would have answered and a stub would only test the stub."""
    git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def commit(repo, message, **files):
    for name, body in files.items():
        path = repo / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize(("name", "expected"), [
    ("ImmutableList.java", {"immutable", "list"}),
    ("parse_config.go", {"parse", "config"} - bt._STOPWORDS),
    ("api/UserSerializer.ts", {"user", "serializer"}),
    ("io.go", set()),                      # too short to be a useful query
    ("src/main/index.js", set()),          # nothing but stopwords
])
def test_a_name_is_split_into_the_words_an_agent_would_search_for(name, expected):
    assert bt.concept_tokens(name) == expected


def test_the_search_finds_a_caller_in_an_unrelated_directory(repo):
    """The whole reason this baseline is hard: an agent greps for a symbol and
    finds the file that uses it, which no directory or filename rule links."""
    commit(repo, "base",
           **{"core__registry.go": "package core\nfunc ResolveHandler() {}\n",
              "web__server.go": "package web\nfunc main() { ResolveHandler() }\n",
              "docs__readme.md": "unrelated prose\n"})
    sha = commit(repo, "next", **{"core__registry.go":
                                  "package core\nfunc ResolveHandler() { x() }\n"})

    found = bt.agent_search(repo, sha, "core/registry.go", 5)
    assert "web/server.go" in found, found
    assert "core/registry.go" not in found, "the seed must never be its own answer"


def test_the_search_reads_the_parent_tree_not_the_commit_being_scored(repo):
    """Grepping the commit's own tree would let a file that this very commit
    edited answer for it -- the file would already contain the reference the
    change introduced. That is leakage, and it inflates the baseline."""
    commit(repo, "base", **{"core__registry.go": "package core\nfunc ResolveHandler() {}\n"})
    # `web/server.go` only mentions the symbol as of the commit being scored.
    sha = commit(repo, "next",
                 **{"core__registry.go": "package core\nfunc ResolveHandler() { x() }\n",
                    "web__server.go": "package web\nfunc main() { ResolveHandler() }\n"})

    assert "web/server.go" not in bt.agent_search(repo, sha, "core/registry.go", 5)


@pytest.mark.parametrize("path", [
    "vendor/github.com/x/y.go", "third_party/zlib/deflate.c",
    "web/node_modules/left-pad/index.js", "pkg/testdata/golden.json",
    "static/app.min.js",
])
def test_vendored_and_generated_paths_are_never_offered(path):
    """An agent ignores these. Scoring their *names* while the grep refuses to
    read their *contents* would credit the baseline for a rule it never ran."""
    assert not bt._wanted(path)


@pytest.mark.parametrize("path", ["src/vendorised/thing.go", "internal/testdata.go"])
def test_a_path_that_merely_looks_vendored_is_kept(path):
    """`vendor/` is a directory, not a substring; excluding by substring would
    silently drop real source files."""
    assert bt._wanted(path)


def test_a_failing_git_call_costs_one_prompt_not_the_whole_run(tmp_path):
    """A replay scores hundreds of thousands of prompts. Letting one slow or
    broken git invocation raise would throw all of that away, so a baseline
    that cannot answer simply misses."""
    assert bt._git(str(tmp_path / "does-not-exist"), ["ls-tree", "HEAD"], timeout=5) == ""
    assert bt.agent_search(tmp_path / "does-not-exist", "deadbeef", "a/b.go", 5) == []


def test_the_same_commit_is_searched_the_same_way_twice(repo):
    """Search terms came off an unordered set, so results moved between runs
    and the benchmark was not reproducible."""
    commit(repo, "base",
           **{"core__registry.go": "package core\nfunc ResolveHandler() {}\nfunc BuildIndex() {}\n",
              "web__server.go": "package web\nfunc main() { ResolveHandler() }\n"})
    sha = commit(repo, "next", **{"core__registry.go":
                                  "package core\nfunc ResolveHandler() { x() }\nfunc BuildIndex() {}\n"})
    bt.concept_tokens.cache_clear()
    first = bt.agent_search(repo, sha, "core/registry.go", 5)
    bt._tree.cache_clear()
    bt.concept_tokens.cache_clear()
    assert bt.agent_search(repo, sha, "core/registry.go", 5) == first


def test_a_root_commit_has_no_parent_to_search(repo):
    sha = commit(repo, "first", **{"core__registry.go": "package core\nfunc ResolveHandler() {}\n"})
    assert bt.agent_search(repo, sha, "core/registry.go", 5) == []


@pytest.mark.parametrize(("path", "stem"), [
    ("internal/auth/token.go", "token"),
    ("internal/auth/token_test.go", "token"),
    ("src/main/java/Auth.java", "auth"),
    ("src/main/java/AuthTest.java", "auth"),
    ("lib/parser.h", "parser"),
    ("lib/parser.cc", "parser"),
    ("spec/user_spec.rb", "user"),
])
def test_a_file_and_its_partner_share_a_stem(path, stem):
    """The cheapest guess anyone makes is the test beside the source, so the
    baseline has to make it too."""
    assert bt.stem_of(path) == stem


# ------------------------------------------------------------------- leakage

def test_a_commit_cannot_inform_its_own_prediction(monkeypatch):
    """The whole point. Files that only ever co-occur in the commit being
    scored must be unpredictable, because at that moment nothing has taught us
    they belong together."""
    history = flat([1, 2], bt.WARMUP_COMMITS + 1)   # noise to get past warmup
    history.append((1, [90, 91]))                        # first and only sighting
    result = replay(monkeypatch, history, measures=("npmi",))

    scored = next(s for s in result.scores if s.measure == "npmi")
    # The 90/91 prompts contribute two predictions and must both miss.
    assert scored.found == 0 or scored.hit_prompts < scored.prompts


def test_a_pair_becomes_predictable_only_after_it_has_been_seen(monkeypatch):
    """Same two files, but now with prior evidence, must be found."""
    history = flat([1, 2], bt.WARMUP_COMMITS)
    history += flat([90, 91], 5)                    # teach the pair
    history.append((1, [90, 91]))                        # then ask
    result = replay(monkeypatch, history, measures=("npmi",))

    scored = next(s for s in result.scores if s.measure == "npmi")
    assert scored.hit_prompts > 0, "a pair seen five times must be predictable"


def test_warmup_commits_are_not_scored(monkeypatch):
    """Predicting from an empty history would only depress the averages."""
    result = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS - 1), measures=("npmi",))
    assert result.prompts == 0
    assert result.verdict.startswith("no prompts")


def test_sweeping_commits_are_not_used_as_prompts(monkeypatch):
    """A 500-file reformat has no 'the file you are editing'."""
    history = flat([1, 2], bt.WARMUP_COMMITS)
    history.append((1, list(range(500, 500 + bt.MAX_FILES_PER_PROMPT + 5))))
    result = replay(monkeypatch, history, measures=("npmi",))
    assert result.prompts == 0


def test_single_file_commits_produce_no_prompt(monkeypatch):
    history = [*flat([1, 2], bt.WARMUP_COMMITS), (1, [7])]
    before = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS), measures=("npmi",)).prompts
    assert replay(monkeypatch, history, measures=("npmi",)).prompts == before


# ------------------------------------------------------------------ baseline

def test_the_baseline_ignores_coupling_entirely(monkeypatch):
    """It answers with the busiest files, which is what makes lift meaningful."""
    history = flat([1, 2], bt.WARMUP_COMMITS + 10)
    result = replay(monkeypatch, history, measures=("npmi",))
    assert {b.measure for b in result.baselines} == {"neighbours", "same directory", "popularity"}
    assert "Apprentice" in result.baseline.label
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
    history += [(1, [90, 91])] * 3
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


def test_counts_are_never_pooled_across_repositories(monkeypatch):
    """Two files in different repositories cannot co-occur. Pooling them hands
    the popularity baseline candidates it can never hit, which does not weaken
    the baseline honestly -- it breaks it, and inflates every lift measured
    against it."""
    a = flat([1, 2], bt.WARMUP_COMMITS + 10, repo=1)
    b = flat([50, 51], bt.WARMUP_COMMITS + 10, repo=2)
    result = replay(monkeypatch, a + b, measures=("npmi",))

    # Each repository's own pair is learnable, so the baseline -- which ranks
    # that repository's busiest files -- must do well rather than near zero.
    assert result.baseline.hit_rate > 0.5, (
        "a pooled population would make the baseline miss almost everything, "
        f"got {result.baseline.hit_rate:.1%}")
    assert result.scores[0].lift <= 1.5


# ------------------------------------------- what history adds over free rules

def test_prompts_the_neighbour_rules_solve_are_excluded_from_the_hard_score(monkeypatch):
    """The headline hit rate flatters every measure, because most prompts are
    answered by the test beside the source. The number worth quoting is what
    survives once those are taken away."""
    # Files 1 and 2 sit in one directory, so the neighbour rule solves them and
    # they must not count towards the hard score.
    history = flat([1, 2], bt.WARMUP_COMMITS + 20)
    result = replay(monkeypatch, history, measures=("npmi",))

    assert result.baseline.measure == "neighbours"
    assert result.baseline.hit_rate > 0.9, "same-directory pairs are free to guess"
    assert result.scores[0].hard_prompts == 0, (
        "every prompt was solved for free, so none is a hard prompt")


def test_a_cross_directory_pair_counts_as_a_hard_prompt(monkeypatch):
    """Files that share neither a name nor a directory are exactly the case
    this product exists for, so they must reach the hard score."""
    paths = {1: "api/handler.py", 2: "web/template.html"}
    history = flat([1, 2], bt.WARMUP_COMMITS + 20)
    result = replay(monkeypatch, history, paths=paths, measures=("npmi",))

    assert result.scores[0].hard_prompts > 0, "no free rule links these two files"
    assert result.scores[0].hard_hit_rate > 0.9, (
        "a pair seen twenty times must be recovered from history")


def test_the_verdict_names_the_baseline_that_actually_won(monkeypatch):
    """It used to say 'popularity' regardless of which baseline was hardest,
    which misreported what the product had been compared against. It now names
    the rung that actually won."""
    # Enough commits to clear MIN_PROMPTS_FOR_A_VERDICT, or the verdict is
    # replaced by the "indicative only" notice and names no baseline at all.
    history = flat([1, 2], bt.WARMUP_COMMITS + bt.MIN_PROMPTS_FOR_A_VERDICT)
    result = replay(monkeypatch, history, measures=("npmi",))
    assert result.conclusive
    assert result.baseline.label.split(" --")[0] in result.verdict


# ------------------------------------------------------------------- seeding

def test_every_changed_file_takes_a_turn_by_default(monkeypatch):
    """A commit touching three files asks three questions, one per file."""
    history = [*flat([1, 2], bt.WARMUP_COMMITS), (1, [1, 2, 3])]
    before = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS), measures=("npmi",)).prompts
    after = replay(monkeypatch, history, measures=("npmi",)).prompts
    assert after - before == 3


def test_obscure_seeding_asks_once_per_commit(monkeypatch):
    """One question per commit, so a commit touching many files cannot dominate
    the sample with its own easy cases."""
    history = [*flat([1, 2], bt.WARMUP_COMMITS), (1, [1, 2, 3])]
    before = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS),
                    measures=("npmi",), seeding="obscure").prompts
    after = replay(monkeypatch, history, measures=("npmi",), seeding="obscure").prompts
    assert after - before == 1


def test_obscure_seeding_starts_from_the_least_changed_file():
    """Seeding from a hub that half the repository moves with makes the rest
    easy to guess. The quiet file is the honest starting point."""
    marginal = {1: 500, 2: 40, 3: 2}
    assert bt._seeds([1, 2, 3], marginal, "obscure") == [3]
    assert bt._seeds([1, 2, 3], marginal, "all") == [1, 2, 3]


def test_a_file_never_seen_before_is_the_most_obscure():
    """Absent from the prior counts means zero, not missing."""
    assert bt._seeds([1, 9], {1: 10}, "obscure") == [9]


def test_an_unknown_seeding_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="unknown seeding"):
        replay(monkeypatch, flat([1, 2], 5), measures=("npmi",), seeding="sideways")


# ---------------------------------------------------- sampling the New Hire

def sampled_commits(monkeypatch, n_commits, sample):
    """Which commits the search baseline actually got run against."""
    seen = []
    history = flat([1, 2], bt.WARMUP_COMMITS) + [(1, [1, 2, 3])] * n_commits
    monkeypatch.setattr(bt, "_commit_shas",
                        lambda repo_id: {i: (f"sha{i}", "org/repo", "github.com")
                                         for i in range(len(history))})
    monkeypatch.setattr(bt, "mirror_path_for",
                        lambda name, host="": bt.Path("/nonexistent"))

    def spy(mirror, sha, seed_path, k):
        seen.append(int(sha.removeprefix("sha")))
        return []
    monkeypatch.setattr(bt, "agent_search", spy)
    replay(monkeypatch, history, measures=("npmi",), grep_sample=sample)
    return seen


def test_the_search_sample_is_drawn_from_the_whole_replay(monkeypatch):
    """It used to sample with a fixed probability and stop at the cap, so the
    entire sample came from the oldest commits while every other measure was
    scored across all of history. That compares two different eras of the
    repository and calls the difference a result."""
    n_commits = 2_000
    seen = sampled_commits(monkeypatch, n_commits, sample=50)

    assert len(seen) == 50
    last_commit = bt.WARMUP_COMMITS + n_commits
    # A front-loaded sample cannot reach the final quarter of the replay.
    assert max(seen) > last_commit * 0.75, (
        f"sample stopped at commit {max(seen)} of {last_commit}")


def test_the_sample_never_exceeds_what_was_asked_for(monkeypatch):
    assert len(sampled_commits(monkeypatch, 500, sample=20)) == 20


def test_a_replay_smaller_than_the_sample_is_taken_whole(monkeypatch):
    seen = sampled_commits(monkeypatch, 5, sample=400)
    assert 0 < len(seen) <= 400


# --------------------------------------------------- what lift is divided by

def test_lift_is_never_divided_by_a_sampled_rate(monkeypatch):
    """Lift is a ratio. Dividing a rate measured over every prompt by one
    measured over a few hundred mixes two estimators, and the figure then moves
    with the draw rather than with the product."""
    history = flat([1, 2], bt.WARMUP_COMMITS + 400)
    monkeypatch.setattr(bt, "_commit_shas",
                        lambda repo_id: {i: (f"sha{i}", "org/repo", "github.com")
                                         for i in range(len(history))})
    monkeypatch.setattr(bt, "mirror_path_for",
                        lambda name, host="": bt.Path("/nonexistent"))
    # A New Hire that answers everything, so it would top the table if allowed.
    monkeypatch.setattr(bt, "agent_search", lambda m, sha, path, k: [path])
    result = replay(monkeypatch, history, measures=("npmi",), grep_sample=50)

    assert any(b.measure == bt.SAMPLED_BASELINE for b in result.baselines)
    assert result.baseline.measure != bt.SAMPLED_BASELINE


def test_beating_the_baseline_requires_the_interval_to_clear_it(monkeypatch):
    """A point estimate one noisy percent above the baseline has not beaten it,
    and the property used to say it had."""
    history = flat([1, 2], bt.WARMUP_COMMITS + 400)
    result = replay(monkeypatch, history, measures=("npmi",))
    s = result.scores[0]
    assert s.baseline_hit_rate == pytest.approx(result.baseline.hit_rate)
    assert s.beats_baseline == (s.ci_low > result.baseline.hit_rate)


def test_the_unaided_rate_is_zero_when_nothing_was_sampled(monkeypatch):
    """Dividing by a sample that was never taken must not invent a rate."""
    result = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS + 400), measures=("npmi",))
    assert result.scores[0].unaided_prompts == 0
    assert result.scores[0].unaided_hit_rate == 0.0
    assert result.sampled == 0


# ------------------------------------------------------- reporting edge cases

def test_the_unaided_interval_is_reported_alongside_the_rate():
    """It is the number with the smallest denominator and the one most likely
    to be over-read, so it must carry its own interval."""
    s = bt.Score(measure="npmi", label="npmi", prompts=100, hit_prompts=50,
                 found=50, wanted=100, hit_rate=0.5, ci_low=0.4, ci_high=0.6,
                 recall_at_k=0.5, precision_at_k=0.1, mrr=0.3,
                 unaided_hits=5, unaided_prompts=20)
    low, high = s.unaided_ci
    assert low < 0.25 < high
    assert (low, high) == bt.wilson(5, 20)


def test_a_run_with_prompts_but_no_measures_says_so(monkeypatch):
    """Scoring nothing is not the same as having no history, and reporting the
    second when the first happened would hide a misconfigured measure list."""
    result = bt.BacktestResult(repo_id=None, k=5, min_support=2, commits_seen=10,
                               commits_scored=5, prompts=40)
    assert result.best is None
    assert result.verdict == "no measures evaluated"


@pytest.mark.parametrize(("path", "stem"), [
    ("test_helpers.go", "helpers"),   # a leading affix, not a trailing one
    ("spec_runner.rb", "runner"),
])
def test_a_leading_test_affix_is_stripped_too(path, stem):
    """`test_helpers.go` is the partner of `helpers.go`; only stripping
    trailing affixes would miss half the convention."""
    assert bt.stem_of(path) == stem


def test_a_seed_with_no_known_path_offers_no_neighbours(monkeypatch):
    """A file id that predates the path index must not raise mid-replay."""
    assert bt._neighbours(seed=999, marginal={}, k=5, paths={}, by_stem={}, by_dir={}) == []
    assert bt._same_directory(seed=999, marginal={}, k=5, paths={}, by_dir={}) == []


def test_a_name_sibling_outranks_the_rest_of_the_directory():
    """The test beside the source is the cheapest and most reliable guess, so
    it has to come first rather than merely be included."""
    paths = {1: "pkg/auth.go", 2: "pkg/auth_test.go", 3: "pkg/unrelated.go"}
    by_stem = {"auth": [1, 2], "unrelated": [3]}
    by_dir = {"pkg": [1, 2, 3]}
    got = bt._neighbours(1, {2: 1, 3: 500}, 5, paths, by_stem, by_dir)
    assert got[0] == 2, "the sibling must outrank a much busier directory-mate"


def test_the_search_declines_a_seed_it_cannot_name(tmp_path):
    """No usable term means no answer, rather than an answer built from noise."""
    work = worktree(tmp_path, "unnamed")
    g(work, "tag", "v1.0.0")
    add_commit(work, "b.txt", "2")
    sha = g(work, "rev-parse", "HEAD").stdout.strip()
    # `io` is below the length floor and `main` is a stopword.
    assert bt.agent_search(work, sha, "pkg/io.go", 5) == []


# --------------------------------------------- reading the replay from the db

@pytest.fixture
def seeded(db):
    """A repository with three commits, one of them not pair-eligible."""
    from datetime import UTC, datetime

    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        old = session.query(models().Repo).filter_by(full_name="acme/replay").one_or_none()
        if old is not None:
            session.query(models().CommitFile).filter_by(repo_id=old.id).delete(synchronize_session=False)
            session.query(models().Commit).filter_by(repo_id=old.id).delete(synchronize_session=False)
            session.query(models().File).filter_by(repo_id=old.id).delete(synchronize_session=False)
            session.delete(old)
            session.flush()
        repo_row = models().Repo(full_name="acme/replay", name="replay", owner="acme")
        session.add(repo_row)
        session.flush()
        repo = repo_row.id
        file_rows = [models().File(repo_id=repo, path=p, dir_path=p.rsplit("/", 1)[0], basename=p.rsplit("/", 1)[-1])
                     for p in ("pkg/a.go", "pkg/a_test.go", "web/b.go")]
        session.add_all(file_rows)
        session.flush()
        files = [row.id for row in file_rows]

        def commit(sha, when, eligible, touched):
            row = models().Commit(repo_id=repo, sha=sha, authored_at=datetime.fromisoformat(when).replace(tzinfo=UTC),
                                  committed_at=datetime.fromisoformat(when).replace(tzinfo=UTC), pair_eligible=eligible)
            session.add(row)
            session.flush()
            cid = row.id
            for f in touched:
                session.add(models().CommitFile(repo_id=repo, commit_id=cid, file_id=f, change_type="M"))
            return cid

        first = commit("a" * 40, "2024-01-01", True, files[:2])
        second = commit("b" * 40, "2024-02-01", True, files)
        commit("c" * 40, "2024-03-01", False, files)      # a sweep: excluded
    yield repo, files, (first, second)


def test_the_replay_reads_commits_oldest_first(seeded, monkeypatch):
    """Prequential scoring depends on the order: a commit scored before its
    predecessors were learned from would be judged on a history that had not
    happened yet."""
    repo, _files, (first, second) = seeded

    history = bt._history(repo)
    assert [cid for _, _, cid in history] == [first, second]


def test_a_commit_excluded_from_pairs_never_becomes_a_prompt(seeded, monkeypatch):
    """`pair_eligible` is the one switch that keeps a sweep out of the
    statistics, so the replay has to honour it at the source."""
    repo, _files, _ = seeded
    assert len(bt._history(repo)) == 2, "the ineligible commit must not appear"


def test_the_replay_carries_the_repository_with_each_commit(seeded, monkeypatch):
    """Counts are kept per repository, so the owner has to travel with the
    commit rather than being looked up later."""
    repo, _, _ = seeded
    assert {r for r, _, _ in bt._history(repo)} == {repo}


def test_commit_shas_are_read_with_their_repository_name(seeded, monkeypatch):
    """The search baseline needs both: the sha to check out, and the name to
    find the mirror on disk."""
    repo, _, (first, _) = seeded
    shas = bt._commit_shas(repo)
    assert shas[first] == ("a" * 40, "acme/replay", "github.com")


def test_the_path_index_groups_by_stem_and_by_directory(seeded, monkeypatch):
    """Both are what the free baselines answer from."""
    repo, files, _ = seeded
    paths, by_stem, by_dir = bt._path_index(repo)

    assert paths[files[0]] == "pkg/a.go"
    assert set(by_stem["a"]) == {files[0], files[1]}, "source and its test share a stem"
    assert set(by_dir["pkg"]) == {files[0], files[1]}


def test_the_search_scores_a_filename_match_without_reading_the_file(tmp_path):
    """The `.*` half of an agent's query: a file whose *name* shares a concept
    is a candidate even when it mentions none of the seed's symbols, which is
    what reaches across directories and languages."""
    work = worktree(tmp_path, "byname")
    (work / "web").mkdir()
    (work / "invoice_loader.py").write_text("def load(): return 1\n")
    # Shares the concept in its name and mentions nothing from the seed. Note
    # `config` would not do here: it is a stopword, because a term appearing in
    # half a repository's files selects nothing.
    (work / "web" / "invoice_renderer.py").write_text("# unrelated contents\n")
    (work / "web" / "unrelated.py").write_text("x = 1\n")
    g(work, "add", "-A")
    g(work, "commit", "--quiet", "-m", "layout")
    add_commit(work, "later.py", "y = 2\n")
    sha = g(work, "rev-parse", "HEAD").stdout.strip()

    found = bt.agent_search(work, sha, "invoice_loader.py", 5)
    assert "web/invoice_renderer.py" in found
    assert "web/unrelated.py" not in found


def test_the_search_widens_using_what_the_first_round_returned(tmp_path):
    """The read-a-result-and-search-again step. Without it the search stops at
    files that mention the seed, and never reaches what those files pull in."""
    work = worktree(tmp_path, "widen")
    (work / "parser.py").write_text("class ParseTree:\n    pass\n")
    # Found in round one: it mentions the seed's symbol.
    (work / "treewalker.py").write_text("from parser import ParseTree\n")
    # Reachable only in round two, via `treewalker`'s own name.
    (work / "helpers.py").write_text("# see treewalker for details\n")
    g(work, "add", "-A")
    g(work, "commit", "--quiet", "-m", "layout")
    add_commit(work, "later.py", "y = 2\n")
    sha = g(work, "rev-parse", "HEAD").stdout.strip()

    found = bt.agent_search(work, sha, "parser.py", 5)
    assert "treewalker.py" in found, "round one must find the caller"
    assert "helpers.py" in found, "round two must follow what round one returned"


def test_grepping_for_no_terms_asks_git_nothing(tmp_path):
    """An empty alternation would match every line in the repository."""
    work = worktree(tmp_path, "noterms")
    assert bt._grep_terms(str(work), "HEAD", []) == {}


def test_grep_output_that_is_not_for_this_commit_is_ignored(tmp_path, monkeypatch):
    """`git grep` prefixes every line with the revision. A line without it did
    not come from the tree being searched."""
    work = worktree(tmp_path, "prefix")
    add_commit(work, "auth.py", "def check(): pass\n")
    sha = g(work, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(bt, "_git", lambda *a, **k: "stray line with no revision\n")
    assert bt._grep_terms(str(work), sha, ["check"]) == {}


def test_a_commit_touching_one_file_yields_no_prompt(monkeypatch):
    """There is no "what else" when nothing else changed, and counting it as a
    miss would punish every measure for a question never asked."""
    history = [*flat([1, 2], bt.WARMUP_COMMITS), (1, [1, 1])]   # one distinct file
    before = replay(monkeypatch, flat([1, 2], bt.WARMUP_COMMITS), measures=("npmi",)).prompts
    assert replay(monkeypatch, history, measures=("npmi",)).prompts == before


def test_prompts_that_defeat_both_free_rules_are_counted_separately(monkeypatch):
    """The only number where this product is not redundant: prompts that
    neither the naming rules nor the agent's own search could answer."""
    paths = {1: "api/handler.py", 2: "web/template.html"}   # no shared stem or folder
    history = flat([1, 2], bt.WARMUP_COMMITS + 400)
    monkeypatch.setattr(bt, "_commit_shas",
                        lambda repo_id: {i: (f"sha{i}", "org/repo", "github.com")
                                         for i in range(len(history))})
    monkeypatch.setattr(bt, "mirror_path_for",
                        lambda name, host="": bt.Path("/nonexistent"))
    monkeypatch.setattr(bt, "agent_search", lambda *a, **k: [])   # the search finds nothing

    result = replay(monkeypatch, history, paths=paths,
                    measures=("npmi",), grep_sample=20)
    best = result.scores[0]
    assert best.unaided_prompts > 0, "neither free rule could answer these"
    assert best.unaided_hit_rate > 0.9, "but history has seen the pair 400 times"
    low, high = best.unaided_ci
    assert low <= best.unaided_hit_rate <= high


def test_a_measure_that_clears_the_baseline_is_reported_with_its_lift():
    def score(measure, label, rate, low, **over):
        return bt.Score(measure=measure, label=label, prompts=bt.MIN_PROMPTS_FOR_A_VERDICT,
                        hit_prompts=0, found=0, wanted=0, hit_rate=rate, ci_low=low,
                        ci_high=rate, recall_at_k=0.0, precision_at_k=0.0, mrr=0.0, **over)

    result = bt.BacktestResult(
        repo_id=None, k=5, min_support=2, commits_seen=10, commits_scored=5,
        prompts=bt.MIN_PROMPTS_FOR_A_VERDICT,
        baselines=[score("intern", "Intern -- same directory", 0.2, 0.18)],
        scores=[score("jaccard", "jaccard", 0.6, 0.55, lift=3.0,
                      hard_prompts=100, hard_hit_rate=0.4)],
    )
    assert result.verdict == ("jaccard hits 60.0% vs 20.0% for the Intern (3.00x); "
                              "on the 100 prompts the free rules missed, it still answers 40.0%")
