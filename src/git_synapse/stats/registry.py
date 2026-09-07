"""Registry describing every association measure: metadata plus the callable.

The registry is the single source of truth about what measures exist. The API,
the MCP server, the UI's metric picker and the aggregation job all enumerate
from here, so adding a 30th measure means adding one function in
:mod:`git_synapse.stats.measures` and one :class:`MeasureSpec` below -- nothing else
in the system needs to change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from git_synapse.stats import measures as m
from git_synapse.stats.contingency import Contingency


class Family(str):
    """Grouping used to organise measures in the UI. Values are display-ready."""

    SIMILARITY = "Similarity & Overlap"
    MATCHING = "Matching Coefficients"
    INFORMATION = "Information Theoretic"
    SIGNIFICANCE = "Significance Tests"
    CORRELATION = "Correlation"
    PROBABILITY = "Probability & Lift"
    DIRECTIONAL = "Directional (asymmetric)"


@dataclass(frozen=True)
class MeasureSpec:
    """Everything the system knows about one association measure.

    Attributes:
        key: stable machine identifier; also the database column name.
        label: human-facing name.
        family: which :class:`Family` it belongs to.
        formula: the formula in terms of the contingency cells a, b, c, d, N.
        summary: one-line description for tooltips and list views.
        detail: longer explanation of when the measure is and is not useful.
        fn: the vectorised implementation.
        lower: lower bound of the range, or None if unbounded.
        upper: upper bound of the range, or None if unbounded.
        signed: True if negative values carry meaning (anti-coupling).
        neutral: the value that indicates exact independence, if one exists.
        is_significance: True for hypothesis tests, which answer "is this real"
            rather than "how strong is this".
        rare_item_bias: True if the measure systematically over-rewards items
            with tiny marginals; the UI warns on these.
        saturates_on_sparse: True for measures that count joint absence and so
            sit near their maximum for almost every commit-data pair.
        zero_when_unobserved: True where the measure returns 0 for a pair that
            never co-occurred, by convention rather than by limit -- PMI's true
            value there is -inf. Declared rather than left implicit because it
            collides with ``neutral``: the same 0 then means both "independent"
            and "never seen together", so a pair observed once can score *below*
            a pair never observed at all. Nothing materialises such a pair --
            support is at least 1 -- but a caller passing its own table can.
        hit_rate: what the reference backtest measured -- the share of prompts
            where a file that really changed appeared in this measure's top 5.
            Not a recommendation. Which question a caller wants asked depends on
            a scenario only the caller knows; this says only how each question
            fared at predicting the next commit.
    """

    key: str
    label: str
    family: str
    formula: str
    summary: str
    detail: str
    fn: Callable[[Contingency], np.ndarray]
    lower: float | None = None
    upper: float | None = None
    signed: bool = False
    neutral: float | None = None
    is_significance: bool = False
    rare_item_bias: bool = False
    saturates_on_sparse: bool = False
    zero_when_unobserved: bool = False
    #: Measured, not asserted. See MEASURED_ON for the corpus.
    hit_rate: float | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def compute(self, t: Contingency) -> np.ndarray:
        """Evaluate this measure over a contingency table."""
        return self.fn(t)


MEASURES: tuple[MeasureSpec, ...] = (
    # ---------------- Similarity & overlap ----------------
    MeasureSpec(
        key="jaccard",
        label="Jaccard Index",
        family=Family.SIMILARITY,
        formula="a / (a + b + c)",
        summary="Overlap of two sets divided by their union.",
        detail=(
            "Measures how much two files' commit sets overlap relative to their "
            "combined size. Ignores joint absence, which is correct for commit "
            "data. Punishes pairs with very unequal change frequencies, so a "
            "stable helper coupled to a churning module scores low even when the "
            "coupling is real."
        ),
        fn=m.jaccard,
        lower=0.0,
        upper=1.0,
    ),
    MeasureSpec(
        key="dice",
        label="Dice Coefficient",
        family=Family.SIMILARITY,
        formula="2a / (2a + b + c)",
        summary="Overlap normalised by the two individual frequencies.",
        detail=(
            "Double-weights the intersection compared to Jaccard, producing "
            "uniformly higher scores. Ranks pairs in exactly the same order as "
            "Jaccard, so choose between them on scale preference, not accuracy."
        ),
        fn=m.dice,
        lower=0.0,
        upper=1.0,
        aliases=("sorensen_dice",),
    ),
    MeasureSpec(
        key="sorensen",
        label="Sorensen Index",
        family=Family.SIMILARITY,
        formula="2a / (2a + b + c)",
        summary="Ecology's name for the Dice coefficient; mathematically identical.",
        detail=(
            "Included as its own entry because the two names dominate different "
            "fields. Values are always exactly equal to the Dice coefficient; a "
            "regression test asserts this."
        ),
        fn=m.sorensen,
        lower=0.0,
        upper=1.0,
    ),
    MeasureSpec(
        key="ochiai",
        label="Ochiai Coefficient",
        family=Family.SIMILARITY,
        formula="a / sqrt(n_a * n_b)",
        summary="Geometric mean of the two conditional probabilities.",
        detail=(
            "Equivalent to cosine similarity on binary vectors. Handles unbalanced "
            "marginals more gracefully than Jaccard because the square root damps "
            "the influence of the more frequent file."
        ),
        fn=m.ochiai,
        lower=0.0,
        upper=1.0,
        aliases=("cosine",),
    ),
    MeasureSpec(
        key="simpson",
        label="Simpson Coefficient (Overlap)",
        family=Family.SIMILARITY,
        formula="a / min(n_a, n_b)",
        summary="Focuses on the size of the smaller set.",
        detail=(
            "Hits 1.0 whenever the rarer file never appears without the other, "
            "making it the best detector of containment ('X is never touched "
            "without Y'). Also the easiest to fool: any file seen exactly once, "
            "alongside anything, scores a perfect 1.0. Always read with a support "
            "threshold."
        ),
        fn=m.simpson,
        lower=0.0,
        upper=1.0,
        rare_item_bias=True,
        aliases=("overlap",),
    ),
    MeasureSpec(
        key="braun_blanquet",
        label="Braun-Blanquet Metric",
        family=Family.SIMILARITY,
        formula="a / max(n_a, n_b)",
        summary="Joint presence against the larger individual frequency.",
        detail=(
            "The conservative mirror of Simpson. Because it normalises by the more "
            "frequent file it cannot be inflated by rare items, at the cost of "
            "under-reporting genuine asymmetric coupling."
        ),
        fn=m.braun_blanquet,
        lower=0.0,
        upper=1.0,
    ),
    MeasureSpec(
        key="kulczynski",
        label="Kulczynski Measure",
        family=Family.SIMILARITY,
        formula="(a/n_a + a/n_b) / 2",
        summary="Arithmetic mean of the two conditional probabilities.",
        detail=(
            "Averages P(B|A) and P(A|B). Because it is an arithmetic rather than "
            "geometric mean it is dominated by whichever conditional is larger, so "
            "it reports strong coupling even when only one direction holds."
        ),
        fn=m.kulczynski,
        lower=0.0,
        upper=1.0,
    ),
    MeasureSpec(
        key="fager",
        label="Fager's Index",
        family=Family.SIMILARITY,
        formula="a / sqrt(n_a * n_b) - 1 / (2 * sqrt(max(n_a, n_b)))",
        summary="Ochiai adjusted by a penalty for small samples.",
        detail=(
            "Subtracts a correction driven by the *commoner* of the two files, "
            "as Fager and McGowan define it: the penalty shrinks as the busier "
            "partner accumulates changes and is flat in the rarer one. It bounds "
            "the optimism of a small sample rather than replacing a support "
            "filter -- a single co-change between two files that each changed "
            "once still scores 0.5, ahead of five co-changes out of ten."
        ),
        fn=m.fager,
        # -0.5, not unbounded: the worst case is n_a = n_b = 1 with a = 0, where
        # ochiai is 0 and the penalty is its largest. Declaring it None meant
        # the bounds test skipped this measure entirely.
        lower=-0.5,
        upper=1.0,
        signed=True,
    ),
    # ---------------- Matching coefficients ----------------
    MeasureSpec(
        key="russell_rao",
        label="Russell-Rao Metric",
        family=Family.MATCHING,
        formula="a / N",
        summary="Joint presence divided by the total sample size.",
        detail=(
            "The raw joint probability, i.e. the fraction of all commits that "
            "touched both files. Tiny for essentially every pair, so treat it as a "
            "support/frequency indicator rather than a strength score."
        ),
        fn=m.russell_rao,
        lower=0.0,
        upper=1.0,
    ),
    MeasureSpec(
        key="sokal_michener",
        label="Sokal-Michener (Simple Matching)",
        family=Family.MATCHING,
        formula="(a + d) / N",
        summary="Counts both joint presence and joint absence.",
        detail=(
            "Treats 'neither file was touched' as evidence of similarity. Since "
            "almost no commit touches any given file, d dominates and this sits "
            "just under 1.0 for nearly every pair. Reported for completeness."
        ),
        fn=m.sokal_michener,
        lower=0.0,
        upper=1.0,
        saturates_on_sparse=True,
    ),
    MeasureSpec(
        key="rogers_tanimoto",
        label="Rogers-Tanimoto Measure",
        family=Family.MATCHING,
        formula="(a + d) / (a + d + 2(b + c))",
        summary="Penalises mismatches twice as heavily as matches.",
        detail=(
            "Simple matching with the disagreement term doubled. Discriminates a "
            "little better than Sokal-Michener but shares its saturation on sparse "
            "commit data."
        ),
        fn=m.rogers_tanimoto,
        lower=0.0,
        upper=1.0,
        saturates_on_sparse=True,
    ),
    MeasureSpec(
        key="hamann",
        label="Hamann Similarity",
        family=Family.MATCHING,
        formula="((a + d) - (b + c)) / N",
        summary="Includes joint absence as a positive correlation factor.",
        detail=(
            "Agreements minus disagreements, on a signed scale. A linear rescaling "
            "of Sokal-Michener, so it carries identical information and the same "
            "sparse-data saturation."
        ),
        fn=m.hamann,
        lower=-1.0,
        upper=1.0,
        signed=True,
        # No neutral value. Hamann is zero iff a + d == b + c, which has nothing
        # to do with independence: the independent tables (4,16,16,64) and
        # (1,9,9,81) score +0.36 and +0.64. Its siblings in this family
        # correctly declare none either.
        saturates_on_sparse=True,
    ),
    MeasureSpec(
        key="faith",
        label="Faith Similarity",
        family=Family.MATCHING,
        formula="(a + 0.5d) / N",
        summary="Weighs mismatches asymmetrically.",
        detail=(
            "Counts joint absence at half the weight of joint presence, sitting "
            "between Jaccard (ignores d) and simple matching (fully counts d)."
        ),
        fn=m.faith,
        lower=0.0,
        upper=1.0,
        saturates_on_sparse=True,
    ),
    # ---------------- Information theoretic ----------------
    MeasureSpec(
        key="mutual_information",
        label="Mutual Information (MI)",
        family=Family.INFORMATION,
        formula="sum_ij p_ij * log2(p_ij / (p_i. * p_.j))",
        summary="Shows if items happen together more than random chance.",
        detail=(
            "Sums surprise across all four cells of the table, weighted by each "
            "cell's probability, so unlike PMI it accounts for the full joint "
            "distribution. Always non-negative and therefore blind to the "
            "direction of the association: pair it with the phi coefficient to "
            "recover whether coupling is positive or negative."
        ),
        fn=m.mutual_information,
        lower=0.0,
        upper=1.0,
    ),
    MeasureSpec(
        key="pmi",
        label="Pointwise Mutual Information (PMI)",
        family=Family.INFORMATION,
        formula="log2(a * N / (n_a * n_b))",
        summary="Ratio of joint probability to independent probability, logged.",
        detail=(
            "Zero at independence, positive above chance, negative below. The "
            "standard lexical-association measure, but heavily biased toward rare "
            "items: two files seen exactly once, together, achieve the maximum "
            "possible score on a single observation."
        ),
        fn=m.pmi,
        zero_when_unobserved=True,
        signed=True,
        neutral=0.0,
        rare_item_bias=True,
    ),
    MeasureSpec(
        key="npmi",
        label="Normalized PMI (NPMI)",
        family=Family.INFORMATION,
        formula="pmi / -log2(a / N)",
        summary="Bounds PMI to [-1, 1] to prevent bias toward rare items.",
        detail=(
            "Dividing by the self-information of the co-occurrence cancels PMI's "
            "rare-item inflation and yields a bounded, comparable score: 1 means "
            "the two files never appear apart, 0 means independence, -1 means they "
            "never appear together. The best general-purpose ranking measure here "
            "for change coupling."
        ),
        fn=m.npmi,
        lower=-1.0,
        upper=1.0,
        signed=True,
        neutral=0.0,
    ),
    MeasureSpec(
        key="ppmi",
        label="Positive PMI (PPMI)",
        family=Family.INFORMATION,
        formula="max(pmi, 0)",
        summary="Replaces negative PMI values with zero.",
        detail=(
            "Standard when scores feed a vector space or embedding, since negative "
            "PMI is estimated from the sparsest region of the table and is mostly "
            "noise. Retains PMI's rare-item bias on the positive side."
        ),
        fn=m.ppmi,
        zero_when_unobserved=True,
        lower=0.0,
        rare_item_bias=True,
    ),
    # ---------------- Significance tests ----------------
    MeasureSpec(
        key="chi_square",
        label="Chi-Square",
        family=Family.SIGNIFICANCE,
        formula="N(ad - bc)^2 / (n_a * n_b * (c+d) * (b+d))",
        summary="Tests if the link between two items is real or random.",
        detail=(
            "Scales with sample size, so values are not comparable across repos of "
            "different sizes -- use phi or Cramer's V for that. The approximation "
            "degrades when expected cell counts fall below about 5, which is the "
            "regime most file pairs occupy; prefer the log-likelihood ratio there."
        ),
        fn=m.chi_square,
        lower=0.0,
        is_significance=True,
    ),
    MeasureSpec(
        key="log_likelihood_ratio",
        label="Log-Likelihood Ratio (G-squared)",
        family=Family.SIGNIFICANCE,
        formula="2 * sum O * ln(O / E)",
        summary="Best for rare events and small sample sizes.",
        detail=(
            "Dunning's G-squared stays well-calibrated where chi-square breaks "
            "down, which makes it the right significance test for the long tail of "
            "rarely-changed files. Asymptotically chi-square with 1 degree of "
            "freedom, so a value above 10.83 corresponds to p < 0.001."
        ),
        fn=m.log_likelihood_ratio,
        lower=0.0,
        is_significance=True,
    ),
    MeasureSpec(
        key="t_score",
        label="T-Score",
        family=Family.SIGNIFICANCE,
        formula="(a - E) / sqrt(a)",
        summary="Measures confidence that a co-occurrence is not random.",
        detail=(
            "Dominated by raw frequency, so it favours pairs with lots of evidence "
            "even when the effect size is modest. This is the exact opposite of "
            "PMI's bias, which makes the two useful to read side by side. Values "
            "above about 2 are conventionally significant."
        ),
        fn=m.t_score,
        signed=True,
        neutral=0.0,
        is_significance=True,
    ),
    MeasureSpec(
        key="z_score",
        label="Z-Score",
        family=Family.SIGNIFICANCE,
        formula="(a - E) / sqrt(E)",
        summary="Standardises co-occurrence deviation using the normal distribution.",
        detail=(
            "Divides by the expected rather than observed count, making it more "
            "sensitive to rare pairs than the t-score. The normal approximation is "
            "weak at very low expected counts; the Poisson measure handles that "
            "case properly."
        ),
        fn=m.z_score,
        signed=True,
        neutral=0.0,
        is_significance=True,
    ),
    MeasureSpec(
        key="poisson_significance",
        label="Poisson Significance",
        family=Family.SIGNIFICANCE,
        formula="-log10 P(X >= a),  X ~ Poisson(E)",
        summary="Uses the Poisson distribution to model random item mixing.",
        detail=(
            "Treats commits as mixing files independently at random and asks how "
            "unlikely the observed co-occurrence count is. Reported as -log10(p) so "
            "that larger is stronger, consistent with every other measure. Better "
            "calibrated than the z-score when expected counts are small."
        ),
        fn=m.poisson_significance,
        lower=0.0,
        is_significance=True,
    ),
    MeasureSpec(
        key="hypergeometric_significance",
        label="Hypergeometric Probability",
        family=Family.SIGNIFICANCE,
        formula="-log10 P(X >= a),  X ~ Hypergeom(N, n_a, n_b)",
        summary="Exact chance of joint occurrence, sampling without replacement.",
        detail=(
            "Fisher's exact test, right tail. Makes no asymptotic approximation "
            "whatsoever, which makes it the most rigorous measure in the registry "
            "and also the most expensive. Best used to confirm candidates that "
            "cheaper measures have already surfaced."
        ),
        fn=m.hypergeometric_significance,
        lower=0.0,
        is_significance=True,
    ),
    # ---------------- Correlation ----------------
    MeasureSpec(
        key="phi",
        label="Phi Coefficient",
        family=Family.CORRELATION,
        formula="(ad - bc) / sqrt(n_a * n_b * (c+d) * (b+d))",
        summary="Correlation coefficient for two binary variables.",
        detail=(
            "The signed counterpart to chi-square (phi^2 = chi2 / N) and the most "
            "directly interpretable correlation available. Negative values are "
            "genuine signal: they identify files that systematically avoid each "
            "other, which usually traces a real module boundary."
        ),
        fn=m.phi,
        lower=-1.0,
        upper=1.0,
        signed=True,
        neutral=0.0,
    ),
    MeasureSpec(
        key="cramers_v",
        label="Cramer's V",
        family=Family.CORRELATION,
        formula="sqrt(chi2 / (N * min(r-1, c-1)))",
        summary="Extension of phi for tables larger than 2x2.",
        detail=(
            "For the 2x2 file-pair case this reduces exactly to |phi|, since "
            "min(r-1, c-1) is 1. It earns its own entry because the directory-level "
            "rollups build larger contingency tables where the reduction no longer "
            "holds. Unlike chi-square it is normalised, so it is comparable across "
            "repositories of different sizes."
        ),
        fn=m.cramers_v,
        lower=0.0,
        upper=1.0,
    ),
    MeasureSpec(
        key="yules_q",
        label="Yule's Q",
        family=Family.CORRELATION,
        formula="(ad - bc) / (ad + bc)",
        summary="Strength of association between two binary attributes.",
        detail=(
            "Reaches +/-1 as soon as any single cell hits zero, so a pair that has "
            "never appeared apart scores a perfect 1.0 on as little as two "
            "observations. Good at expressing direction, poor at expressing "
            "confidence."
        ),
        fn=m.yules_q,
        lower=-1.0,
        upper=1.0,
        signed=True,
        neutral=0.0,
        rare_item_bias=True,
    ),
    MeasureSpec(
        key="yules_y",
        label="Yule's Y",
        family=Family.CORRELATION,
        formula="(sqrt(ad) - sqrt(bc)) / (sqrt(ad) + sqrt(bc))",
        summary="Coefficient of colligation; a damped variation of Yule's Q.",
        detail=(
            "The square roots pull values away from the extremes, so Yule's Y "
            "discriminates among strongly coupled pairs where Q has already "
            "saturated at 1.0."
        ),
        fn=m.yules_y,
        lower=-1.0,
        upper=1.0,
        signed=True,
        neutral=0.0,
    ),
    MeasureSpec(
        key="michael",
        label="Michael's Measure",
        family=Family.CORRELATION,
        formula="4(ad - bc) / ((a + d)^2 + (b + c)^2)",
        summary="Non-linear variation of the chi-square statistic.",
        detail=(
            "Shares the ad - bc numerator with phi and Yule's Q but normalises by "
            "squared sums. Because d enters the denominator it behaves like a "
            "matching coefficient on sparse data and compresses hard toward zero."
        ),
        fn=m.michael,
        lower=-1.0,
        upper=1.0,
        signed=True,
        neutral=0.0,
        saturates_on_sparse=True,
    ),
    # ---------------- Probability / lift ----------------
    MeasureSpec(
        key="association_strength",
        label="Association Strength",
        family=Family.PROBABILITY,
        formula="a * N / (n_a * n_b)",
        summary="Uses probability to see how strong a pair bond is.",
        detail=(
            "Observed co-occurrence divided by what independence predicts, so 1.0 "
            "is exactly chance and 14.0 means 'these change together fourteen times "
            "more often than chance'. The unlogged sibling of PMI, and far easier "
            "to state to a human, though it inherits the same rare-item bias."
        ),
        fn=m.association_strength,
        lower=0.0,
        neutral=1.0,
        rare_item_bias=True,
        aliases=("lift",),
    ),
    # ---------------- Directional extras (not part of the 29) ----------------
    MeasureSpec(
        key="confidence_ab",
        label="Confidence P(B|A)",
        family=Family.DIRECTIONAL,
        formula="a / n_a",
        summary="Given that A changed, how often B changed too.",
        detail=(
            "Asymmetric, and the single most actionable number for a coding agent: "
            "'when you touch A, B also changes 80% of the time'. Not one of the 29 "
            "symmetric measures, but computed and stored alongside them."
        ),
        fn=m.confidence_ab,
        lower=0.0,
        upper=1.0,
    ),
    MeasureSpec(
        key="confidence_ba",
        label="Confidence P(A|B)",
        family=Family.DIRECTIONAL,
        formula="a / n_b",
        summary="Given that B changed, how often A changed too.",
        detail=(
            "The reverse direction of confidence. Comparing the two exposes "
            "one-way dependencies: a generated file may always accompany its "
            "schema, while the schema often changes alone."
        ),
        fn=m.confidence_ba,
        lower=0.0,
        upper=1.0,
    ),
)

BY_KEY: dict[str, MeasureSpec] = {spec.key: spec for spec in MEASURES}

# Alias -> canonical key, so the API accepts "lift" or "cosine" too.
_ALIASES: dict[str, str] = {
    alias: spec.key for spec in MEASURES for alias in spec.aliases
}

#: The symmetric measures: every family except DIRECTIONAL. Scoring persists
#: ALL_KEYS, so this is not a subset anything stores -- it is the set for which
#: `m(A,B) == m(B,A)` holds, which is what the symmetry tests parametrise over.
CORE_KEYS: tuple[str, ...] = tuple(
    spec.key for spec in MEASURES if spec.family != Family.DIRECTIONAL
)

#: Every key that gets a materialised column, including the directional extras.
ALL_KEYS: tuple[str, ...] = tuple(spec.key for spec in MEASURES)

#: Sensible default when a caller does not name a measure.
#: Chosen by measurement, not taste, and the claim is narrower than it once was.
#: Backtested over 212,269 commits across six organisations and six languages,
#: P(B|A) ranks first among the measures on every corpus tried. Against the
#: hardest free baseline -- the file's test, then its folder -- that is 1.18x
#: corpus-wide, not the 1.6x-3.9x once quoted here: those figures were measured
#: against "the repository's busiest files", which nobody has ever used to
#: decide what to open. On a meticulously organised codebase the free rule wins
#: outright. See `git-synapse backtest`.
#:
#: The result is principled rather than lucky: "what else must change" asks for
#: the probability B changes given A did, which is exactly what this computes.
#: The symmetric measures answer "is this association surprising?" -- a better
#: question for discovery, a worse one for prediction.
DEFAULT_MEASURE = "confidence_ab"

#: The corpus every `hit_rate` and `lift` on a MeasureSpec was measured over, so
#: a caller can weigh how much the number should travel. Different code has
#: different habits: the figures move by repository, and on the most
#: convention-regular ones every measure loses to guessing the file's test.
MEASURED_ON = ("471,972 predictions over 105,986 commits in 79 repositories "
               "across six organisations and six languages")

#: What the same corpus yields with no history at all: the file's test, then the
#: rest of its folder. Reported beside every measured hit rate so the two can be
#: compared without a ratio anyone has to interpret. A measure below it is one
#: worth ignoring -- and on the most convention-regular repositories, every
#: measure is below it.
FREE_LOOKUP_HIT_RATE = 0.534

#: What each measure scored on that corpus. Reported rather than ranked: the
#: order here answers one question -- what else changes with this file -- and a
#: caller asking a different one should read the formula, not this number.
#:
#: Note what the top of the list means. `confidence_ab` is `a / n_a` and
#: `russell_rao` is `a / N`; when ranking one file's partners both denominators
#: are constant, so both sort by `a` alone and score identically. The measure
#: that wins this benchmark is arithmetically "how often did these two change
#: together", and the other thirty earn their place on other questions, not on
#: this one.
_MEASURED_HIT_RATE = {
    "russell_rao": 0.632,
    "confidence_ab": 0.632,
    "michael": 0.626,
    "t_score": 0.615,
    "fager": 0.596,
    "jaccard": 0.589,
    "dice": 0.589,
    "sorensen": 0.589,
    "braun_blanquet": 0.586,
    "ochiai": 0.584,
    "chi_square": 0.574,
    "cramers_v": 0.574,
    "phi": 0.574,
    "z_score": 0.571,
    "npmi": 0.543,
    "kulczynski": 0.532,
    "faith": 0.514,
    "sokal_michener": 0.467,
    "rogers_tanimoto": 0.467,
    "hamann": 0.467,
    "simpson": 0.464,
    "yules_y": 0.442,
    "yules_q": 0.442,
    "pmi": 0.407,
    "confidence_ba": 0.407,
    "ppmi": 0.407,
}

for _spec in MEASURES:
    object.__setattr__(_spec, "hit_rate", _MEASURED_HIT_RATE.get(_spec.key))


def resolve(key: str) -> MeasureSpec:
    """Look up a measure by key or alias.

    Raises:
        KeyError: if the name matches no measure, with the valid keys listed.
    """
    normalised = key.strip().lower()
    canonical = _ALIASES.get(normalised, normalised)
    if canonical not in BY_KEY:
        raise KeyError(
            f"unknown measure {key!r}; expected one of: {', '.join(sorted(BY_KEY))}"
        )
    return BY_KEY[canonical]


def families() -> dict[str, list[MeasureSpec]]:
    """Group the measures by family, preserving registry order within each."""
    grouped: dict[str, list[MeasureSpec]] = {}
    for spec in MEASURES:
        grouped.setdefault(spec.family, []).append(spec)
    return grouped


def compute_all(t: Contingency, keys: tuple[str, ...] = ALL_KEYS) -> dict[str, np.ndarray]:
    """Evaluate many measures over one contingency table in a single pass.

    Args:
        t: the contingency table (may hold millions of pairs).
        keys: which measures to compute; defaults to every registered measure.

    Returns:
        Mapping of measure key to a float64 array aligned with ``t``.
    """
    return {key: BY_KEY[key].compute(t) for key in keys}
