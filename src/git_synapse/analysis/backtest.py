"""Does this actually help? Answered against history itself.

Every other number in this system describes the corpus. This one describes the
*product*: replay history commit by commit and ask, before each commit is
revealed, "given one file this commit touched, would we have named the others?"

Method
------
Strictly prequential -- test, then train. Walking commits in time order, each
commit is first scored using only the counts accumulated from earlier commits,
and only afterwards do its own pairs update those counts. A file pair therefore
never contributes evidence to its own prediction, which is the whole difficulty
in evaluating a co-change model, and the reason the materialised
``file_pair_metric`` table cannot be used here: it is computed over all history,
so every query against it has already seen the future.

Reading the result honestly
---------------------------
Three things are reported next to every measure, because recall alone is a
vanity metric:

* **Lift over a popularity baseline** that ignores coupling and always answers
  with the busiest files so far. A repository where `go.mod` and `go.sum` move
  together constantly scores well by guessing. Lift at or below 1.0 means the
  statistics earned nothing over that guess.
* **A 95% confidence interval** on the hit rate, so a difference between two
  measures is not read as real when the sample cannot support it.
* **Whether the measure is known to over-reward rare items**, which is exactly
  how a measure tops this table while being useless in practice.

The interval assumes independent trials. Prompts drawn from the same commit are
not independent, so the true interval is somewhat wider than the one reported.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

from git_synapse.db.engine import query
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import DEFAULT_MEASURE, resolve

log = logging.getLogger(__name__)

#: Commits observed before scoring begins. With no history there is nothing to
#: predict from, and those degenerate commits would otherwise drag every
#: average toward zero and make two runs incomparable.
WARMUP_COMMITS = 20

#: Commits touching more files than this are skipped as prompts. A sweeping
#: change has no single "the file you are editing", so asking the question of it
#: measures nothing about the product.
MAX_FILES_PER_PROMPT = 25

#: Below this many prompts the intervals are too wide to separate anything, and
#: the result is reported as indicative rather than as a finding.
MIN_PROMPTS_FOR_A_VERDICT = 300


def wilson(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval, which stays sane at proportions near 0 and 1."""
    if trials == 0:
        return (0.0, 0.0)
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True)
class Score:
    """How one measure fared over the whole replay."""

    measure: str
    label: str
    prompts: int
    #: Prompts where at least one correct file appeared in the top k.
    hit_prompts: int
    #: Correct files found, counting every one, not just the first.
    found: int
    wanted: int
    hit_rate: float
    ci_low: float
    ci_high: float
    recall_at_k: float
    precision_at_k: float
    mrr: float
    lift: float = 0.0
    rare_item_bias: bool = False

    @property
    def beats_baseline(self) -> bool:
        """True only when the interval clears the baseline, not merely the point."""
        return self.lift > 1.0


@dataclass(frozen=True)
class BacktestResult:
    """The outcome of one replay, across every measure evaluated."""

    repo_id: int | None
    k: int
    min_support: int
    commits_seen: int
    commits_scored: int
    prompts: int
    baseline: Score
    scores: list[Score] = field(default_factory=list)

    @property
    def conclusive(self) -> bool:
        """False when the sample is too small to support any claim."""
        return self.prompts >= MIN_PROMPTS_FOR_A_VERDICT

    @property
    def best(self) -> Score | None:
        """The measure with the highest hit rate, or None if nothing scored."""
        return max(self.scores, key=lambda s: s.hit_rate, default=None)

    @property
    def verdict(self) -> str:
        """One line stating what this run does and does not establish."""
        if not self.prompts:
            return "no prompts: not enough history to replay"
        best = self.best
        if best is None:
            return "no measures evaluated"
        if not self.conclusive:
            return (f"indicative only: {self.prompts} prompts is below the "
                    f"{MIN_PROMPTS_FOR_A_VERDICT} needed to separate measures")
        if best.ci_low <= self.baseline.hit_rate:
            return (f"no measure beats guessing the busiest files "
                    f"({self.baseline.hit_rate:.1%}) by more than noise")
        return (f"{best.label} hits {best.hit_rate:.1%} of the time "
                f"vs {self.baseline.hit_rate:.1%} for popularity "
                f"({best.lift:.2f}x)")


def _history(repo_id: int | None) -> list[list[int]]:
    """Pair-eligible commits in time order, each as its list of file ids."""
    where = "WHERE c.pair_eligible" + ("" if repo_id is None else " AND c.repo_id = %(repo)s")
    rows = query(
        f"""
        SELECT c.id AS commit_id, cf.file_id
          FROM commit c JOIN commit_file cf ON cf.commit_id = c.id
          {where}
      ORDER BY c.committed_at, c.id, cf.file_id
        """,
        {"repo": repo_id},
    )
    grouped: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        grouped[row["commit_id"]].append(row["file_id"])
    return list(grouped.values())


def _rank(seed: int, joint: dict[int, dict[int, int]], marginal: dict[int, int], total: int, spec, k: int, min_support: int) -> list[int]:
    """The k partners this measure would have offered for `seed`."""
    partners = joint.get(seed)
    if not partners:
        return []
    ids = [p for p, n in partners.items() if n >= min_support]
    if not ids:
        return []

    n_ab = np.array([partners[p] for p in ids], dtype=np.float64)
    n_b = np.array([marginal[p] for p in ids], dtype=np.float64)
    n_a = np.full(len(ids), float(marginal[seed]))
    raw = spec.compute(Contingency.from_counts(n_ab, n_a, n_b, float(total)))
    scores = np.nan_to_num(np.asarray(raw, dtype=np.float64), nan=-np.inf, neginf=-np.inf)
    return [ids[i] for i in np.argsort(-scores)[:k]]


def _popular(marginal: dict[int, int], seed: int, k: int) -> list[int]:
    """The baseline's answer: the busiest files so far, coupling ignored."""
    ranked = sorted(marginal.items(), key=lambda kv: -kv[1])
    return [f for f, _ in ranked if f != seed][:k]


def run(repo_id: int | None = None, measures: tuple[str, ...] = (DEFAULT_MEASURE,), k: int = 5, min_support: int = 2, limit: int | None = None) -> BacktestResult:
    """Replay history and report how often each measure named the right files.

    Args:
        repo_id: restrict to one repository, or None for the whole corpus.
        measures: measure keys to evaluate side by side.
        k: how many suggestions the product is allowed to offer.
        min_support: ignore partners seen together fewer times than this.
        limit: stop after this many commits, for a quick look.

    Returns:
        A :class:`BacktestResult`, carrying each measure's lift over the
        popularity baseline and a confidence interval on its hit rate.
    """
    specs = [resolve(m) for m in measures]
    commits = _history(repo_id)
    if limit is not None:
        commits = commits[:limit]

    joint: dict[int, dict[int, int]] = defaultdict(dict)
    marginal: dict[int, int] = defaultdict(int)
    total = 0

    zero = {s.key: 0 for s in specs}
    hit_prompts, found = dict(zero), dict(zero)
    recall = {s.key: 0.0 for s in specs}
    precision = {s.key: 0.0 for s in specs}
    rr = {s.key: 0.0 for s in specs}
    base_hit_prompts, base_found, base_recall = 0, 0, 0.0
    prompts, wanted, scored_commits = 0, 0, 0

    for files in commits:
        # ---- test, using only what earlier commits taught -------------------
        if total >= WARMUP_COMMITS and 2 <= len(files) <= MAX_FILES_PER_PROMPT:
            scored_here = False
            for seed in files:
                targets = set(files) - {seed}
                if not targets:
                    continue
                prompts += 1
                wanted += len(targets)
                scored_here = True

                for spec in specs:
                    got = _rank(seed, joint, marginal, total, spec, k, min_support)
                    correct = [f for f in got if f in targets]
                    found[spec.key] += len(correct)
                    hit_prompts[spec.key] += 1 if correct else 0
                    recall[spec.key] += len(correct) / len(targets)
                    precision[spec.key] += len(correct) / k
                    rank = next((i for i, f in enumerate(got, 1) if f in targets), 0)
                    rr[spec.key] += 1.0 / rank if rank else 0.0

                base = [f for f in _popular(marginal, seed, k) if f in targets]
                base_found += len(base)
                base_hit_prompts += 1 if base else 0
                base_recall += len(base) / len(targets)
            scored_commits += 1 if scored_here else 0

        # ---- then train -----------------------------------------------------
        for a, b in combinations(sorted(set(files)), 2):
            joint[a][b] = joint[a].get(b, 0) + 1
            joint[b][a] = joint[b].get(a, 0) + 1
        for f in set(files):
            marginal[f] += 1
        total += 1

    n = prompts or 1
    b_low, b_high = wilson(base_hit_prompts, prompts)
    baseline = Score(
        measure="popularity", label="Most-changed files (baseline)", prompts=prompts,
        hit_prompts=base_hit_prompts, found=base_found, wanted=wanted,
        hit_rate=base_hit_prompts / n, ci_low=b_low, ci_high=b_high,
        recall_at_k=base_recall / n, precision_at_k=base_found / (n * k), mrr=0.0,
    )

    scores = []
    for s in specs:
        low, high = wilson(hit_prompts[s.key], prompts)
        rate = hit_prompts[s.key] / n
        scores.append(Score(
            measure=s.key, label=s.label, prompts=prompts,
            hit_prompts=hit_prompts[s.key], found=found[s.key], wanted=wanted,
            hit_rate=rate, ci_low=low, ci_high=high,
            recall_at_k=recall[s.key] / n, precision_at_k=precision[s.key] / n,
            mrr=rr[s.key] / n,
            lift=rate / baseline.hit_rate if baseline.hit_rate else 0.0,
            rare_item_bias=s.rare_item_bias,
        ))
    scores.sort(key=lambda s: -s.hit_rate)

    result = BacktestResult(repo_id, k, min_support, len(commits), scored_commits, prompts, baseline, scores)
    log.info("backtest: %d commits, %d prompts -- %s", len(commits), prompts, result.verdict)
    return result
