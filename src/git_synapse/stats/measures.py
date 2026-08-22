"""The 29 association measures, each a pure function of a 2x2 contingency table.

Design notes
------------
Every measure takes a :class:`~git_synapse.stats.contingency.Contingency` and returns
a ``float64`` numpy array of the same shape. They are pure, vectorised and free
of database or I/O concerns, which makes them trivially unit-testable against
hand-computed values (see ``tests/test_measures.py``).

The measures fall into families that behave very differently, and picking the
right one matters more than computing all of them:

* **Similarity / overlap** (Jaccard, Dice, Ochiai, Simpson, ...) -- bounded,
  intuitive, but blind to how surprising an overlap is.
* **Information theoretic** (MI, PMI, NPMI, PPMI) -- measure surprise, but PMI
  is notoriously biased toward rare items; NPMI exists to fix exactly that.
* **Significance tests** (chi-square, G², t-score, z-score, Poisson,
  hypergeometric) -- answer "is this real?" rather than "is this strong?".
* **Correlation** (phi, Cramer's V, Yule's Q/Y, Michael) -- signed, so they can
  express *negative* coupling (files that systematically do NOT change together).
* **Matching coefficients** (Sokal-Michener, Rogers-Tanimoto, Hamann, Faith,
  Russell-Rao) -- these count joint *absence* ``d`` as evidence. For commit data
  ``d`` is enormous (almost no commit touches any given file), so these
  saturate near 1.0 and are of limited use on their own. They are included for
  completeness and flagged as such in the registry.
"""

from __future__ import annotations

import numpy as np
from scipy import stats as sp_stats

from git_synapse.stats.contingency import Contingency, safe_div, xlog2y, xlogy

# Significance measures are reported as -log10(p) so that "bigger is stronger"
# holds uniformly across every measure in the registry. p=0 (underflow) is
# clamped here rather than becoming inf.
MAX_NEG_LOG10_P = 300.0


# --------------------------------------------------------------------------
# Similarity / overlap family
# --------------------------------------------------------------------------


def jaccard(t: Contingency) -> np.ndarray:
    """Jaccard index: intersection over union, ``a / (a + b + c)``.

    Range [0, 1]. Ignores joint absence entirely, which is the right call for
    commit data. Penalises pairs where one file changes far more often than the
    other, so a small utility file coupled to a churny one scores low.
    """
    return safe_div(t.a, t.a + t.b + t.c)


def dice(t: Contingency) -> np.ndarray:
    """Dice coefficient: ``2a / (2a + b + c)``.

    Range [0, 1]. Weights the intersection twice, so it is systematically more
    generous than Jaccard (the two are monotonically related and always rank
    pairs identically -- Dice just spreads the scores differently).
    """
    return safe_div(2.0 * t.a, 2.0 * t.a + t.b + t.c)


def sorensen(t: Contingency) -> np.ndarray:
    """Sorensen index -- mathematically identical to :func:`dice`.

    Kept as its own entry because the user's specification lists both, and
    because the two names dominate different literatures (Sorensen in ecology,
    Dice in information retrieval). Any divergence between this and ``dice``
    would be a bug.
    """
    return dice(t)


def ochiai(t: Contingency) -> np.ndarray:
    """Ochiai coefficient: ``a / sqrt(n_a * n_b)``.

    The geometric mean of the two conditional probabilities P(A|B) and P(B|A),
    and identical to cosine similarity on binary vectors. Range [0, 1]. More
    robust than Jaccard when the two marginals are very unbalanced.
    """
    return safe_div(t.a, np.sqrt(t.n_a * t.n_b))


def simpson(t: Contingency) -> np.ndarray:
    """Simpson / overlap coefficient: ``a / min(n_a, n_b)``.

    Range [0, 1]. Reaches 1.0 whenever the rarer file *always* co-occurs with
    the commoner one, regardless of how common the latter is. Excellent for
    finding "X is never touched without Y" containment relationships, but it
    must be read alongside a significance measure or it will surface every
    file that has only ever appeared once.
    """
    return safe_div(t.a, np.minimum(t.n_a, t.n_b))


def braun_blanquet(t: Contingency) -> np.ndarray:
    """Braun-Blanquet: ``a / max(n_a, n_b)``.

    The conservative mirror of Simpson. Range [0, 1]. Only scores high when the
    co-occurrence is large relative to the *more* frequent item, so it is much
    harder to fool with rare files.
    """
    return safe_div(t.a, np.maximum(t.n_a, t.n_b))


def kulczynski(t: Contingency) -> np.ndarray:
    """Kulczynski measure: arithmetic mean of ``P(A|B)`` and ``P(B|A)``.

    ``(a/n_a + a/n_b) / 2``. Range [0, 1]. Sits between Simpson and
    Braun-Blanquet, and unlike Ochiai it is dominated by the larger of the two
    conditionals rather than balanced between them.
    """
    return 0.5 * (safe_div(t.a, t.n_a) + safe_div(t.a, t.n_b))


def fager(t: Contingency) -> np.ndarray:
    """Fager's index: Ochiai minus a small-sample penalty.

    ``a / sqrt(n_a * n_b) - 1 / (2 * sqrt(max(n_a, n_b)))``.

    The correction term shrinks as the rarer item accumulates observations, so
    a pair seen twice is punished hard while a pair seen two hundred times is
    barely touched. This makes it one of the better-behaved similarity measures
    for the long tail of rarely-changed files, which is most of any repository.
    """
    return ochiai(t) - safe_div(1.0, 2.0 * np.sqrt(np.maximum(t.n_a, t.n_b)))


# --------------------------------------------------------------------------
# Matching-coefficient family (these count joint absence ``d``)
# --------------------------------------------------------------------------


def russell_rao(t: Contingency) -> np.ndarray:
    """Russell-Rao: ``a / N``.

    The raw joint probability. Range [0, 1] but in practice microscopic for
    commit data, since any given file pair appears in a vanishing fraction of
    all commits. Useful as a support/frequency signal, not as a strength one.
    """
    return safe_div(t.a, t.n)


def sokal_michener(t: Contingency) -> np.ndarray:
    """Sokal-Michener simple matching: ``(a + d) / N``.

    Counts agreement of both kinds -- both files present, or both absent. For
    sparse commit data ``d`` dominates so this sits just below 1.0 for nearly
    every pair. Included for completeness; see the module docstring.
    """
    return safe_div(t.a + t.d, t.n)


def rogers_tanimoto(t: Contingency) -> np.ndarray:
    """Rogers-Tanimoto: ``(a + d) / (a + d + 2(b + c))``.

    Simple matching with mismatches weighted double. Range [0, 1]. Shares the
    joint-absence saturation problem but discriminates slightly better than
    Sokal-Michener because the disagreement term is amplified.
    """
    return safe_div(t.a + t.d, t.a + t.d + 2.0 * (t.b + t.c))


def hamann(t: Contingency) -> np.ndarray:
    """Hamann similarity: ``((a + d) - (b + c)) / N``.

    Range [-1, 1]. Agreements minus disagreements. Equivalent to
    ``2 * sokal_michener - 1``, so it carries the same information on a signed
    scale.
    """
    return safe_div((t.a + t.d) - (t.b + t.c), t.n)


def faith(t: Contingency) -> np.ndarray:
    """Faith similarity: ``(a + 0.5 * d) / N``.

    Range [0, 1]. Treats joint absence as half as informative as joint
    presence -- an asymmetric compromise between Jaccard (which ignores ``d``)
    and simple matching (which fully counts it).
    """
    return safe_div(t.a + 0.5 * t.d, t.n)


# --------------------------------------------------------------------------
# Information-theoretic family
# --------------------------------------------------------------------------


def pmi(t: Contingency) -> np.ndarray:
    """Pointwise mutual information, in bits: ``log2(a*N / (n_a * n_b))``.

    Zero means the pair co-occurs exactly as often as chance predicts, positive
    means more often, negative means less. Unbounded in both directions and
    strongly biased toward rare items: a pair of files each seen once, together,
    attains the maximum possible PMI on no evidence at all. Always pair it with
    a support threshold or with :func:`npmi`.

    Returns 0 where ``a == 0`` (undefined in the limit, but 0 is the
    conventional and useful choice for ranking).
    """
    ratio = safe_div(t.a * t.n, t.n_a * t.n_b)
    out = np.zeros_like(ratio)
    ok = ratio > 0
    out[ok] = np.log2(ratio[ok])
    return out


def npmi(t: Contingency) -> np.ndarray:
    """Normalised PMI: ``pmi / -log2(a / N)``.

    Bounded to [-1, 1], where 1 means perfect co-occurrence, 0 means
    independence and -1 means the two never co-occur. The normalisation
    directly cancels PMI's rare-item bias, which makes NPMI the best
    general-purpose ranking measure in this registry for change coupling.

    Two limits are handled explicitly because the ratio is 0/0 at both:

    * ``a == 0`` -- the pair never co-occurs. PMI diverges to -inf while the
      denominator also diverges, and the limit of the ratio is exactly -1.
      This is Bouma's convention and it matters here: without it, "never
      change together" and "change together exactly as often as chance"
      would both score 0 and become indistinguishable.
    * ``a == N`` -- both files appear in every single commit, so they always
      co-occur and the limit is +1. Degenerate, but bounded.
    """
    p_ab = safe_div(t.a, t.n)
    denom = np.zeros_like(p_ab)
    interior = (p_ab > 0) & (p_ab < 1)
    denom[interior] = -np.log2(p_ab[interior])

    out = safe_div(pmi(t), denom)

    # a == 0 with both files actually present somewhere => perfect exclusion.
    never = (t.a <= 0) & (t.n_a > 0) & (t.n_b > 0) & (t.n > 0)
    out = np.where(never, -1.0, out)

    # a == N => both files in every commit; always together.
    always = (t.n > 0) & (t.a >= t.n)
    out = np.where(always, 1.0, out)

    return np.clip(out, -1.0, 1.0)


def ppmi(t: Contingency) -> np.ndarray:
    """Positive PMI: ``max(pmi, 0)``.

    Discards negative associations. Standard practice when the scores feed a
    vector space or embedding, because negative PMI is estimated from the
    sparsest part of the table and is mostly noise.
    """
    return np.maximum(pmi(t), 0.0)


def mutual_information(t: Contingency) -> np.ndarray:
    """Mutual information of the full 2x2 table, in bits.

    Unlike PMI (which scores a single cell) this sums over all four cells,
    weighting each by its own probability::

        MI = sum_ij p_ij * log2(p_ij / (p_i. * p_.j))

    Range [0, 1] bits for a 2x2 table. Always non-negative, so it measures how
    much knowing about one file tells you about the other *in either direction*
    -- it cannot distinguish positive from negative coupling. Read it with
    :func:`phi` to recover the sign.
    """
    n = t.n
    cells = (t.a, t.b, t.c, t.d)
    row = (t.n_a, t.n_a, t.n - t.n_a, t.n - t.n_a)
    col = (t.n_b, t.n - t.n_b, t.n_b, t.n - t.n_b)

    total = np.zeros_like(t.a)
    for obs, r, c in zip(cells, row, col):
        p = safe_div(obs, n)
        expected_p = safe_div(r * c, n * n)
        total = total + xlog2y(p, safe_div(p, expected_p, fill=1.0))
    return np.maximum(total, 0.0)


# --------------------------------------------------------------------------
# Significance-test family
# --------------------------------------------------------------------------


def chi_square(t: Contingency) -> np.ndarray:
    """Pearson's chi-square for a 2x2 table.

    Uses the closed form ``N(ad - bc)^2 / (n_a * n_b * (c+d) * (b+d))``.
    Unsigned and unbounded above; roughly ``N * phi^2``. Scales with sample
    size, so a huge chi-square on a huge repository is not directly comparable
    to one from a small repository -- use :func:`phi` or :func:`cramers_v` when
    comparing across repos.

    Unreliable when expected cell counts drop below ~5, which is exactly the
    regime most file pairs live in. :func:`log_likelihood_ratio` is the better
    choice there.
    """
    num = t.n * np.square(t.a * t.d - t.b * t.c)
    den = t.n_a * t.n_b * (t.c + t.d) * (t.b + t.d)
    return safe_div(num, den)


def log_likelihood_ratio(t: Contingency) -> np.ndarray:
    """Log-likelihood ratio G², ``2 * sum O * ln(O / E)`` over all four cells.

    The measure of choice for rare events and small samples, where the
    chi-square approximation breaks down. Dunning's classic result is that G²
    stays well-behaved when expected counts fall below 5, which covers most
    file pairs in a repository.

    Unsigned and unbounded above. Asymptotically chi-square with 1 df, so
    G² > 10.83 corresponds to p < 0.001.
    """
    n = t.n
    cells = (t.a, t.b, t.c, t.d)
    row = (t.n_a, t.n_a, t.n - t.n_a, t.n - t.n_a)
    col = (t.n_b, t.n - t.n_b, t.n_b, t.n - t.n_b)

    total = np.zeros_like(t.a)
    for obs, r, c in zip(cells, row, col):
        expected = safe_div(r * c, n)
        total = total + xlogy(obs, safe_div(obs, expected, fill=1.0))
    return np.maximum(2.0 * total, 0.0)


def t_score(t: Contingency) -> np.ndarray:
    """T-score: ``(a - E) / sqrt(a)`` where ``E = n_a * n_b / N``.

    Measures confidence that the co-occurrence is not chance. Dominated by
    frequency -- high-frequency pairs win even at modest effect size -- which
    makes it a good complement to PMI's opposite bias. Values above ~2 are
    conventionally treated as significant.
    """
    return safe_div(t.a - t.expected, np.sqrt(t.a))


def z_score(t: Contingency) -> np.ndarray:
    """Z-score: ``(a - E) / sqrt(E)``.

    Standardises the deviation from expectation against the standard deviation
    of a Poisson variable with mean E. More sensitive to rare pairs than
    :func:`t_score` because the denominator uses expected rather than observed
    frequency.
    """
    return safe_div(t.a - t.expected, np.sqrt(t.expected))


def poisson_significance(t: Contingency) -> np.ndarray:
    """Poisson tail significance, reported as ``-log10 P(X >= a)``.

    Models co-occurrence counts as Poisson with rate ``E = n_a * n_b / N``,
    which is the natural null when commits mix files independently at random.
    Higher means less likely under chance. Clamped to
    ``MAX_NEG_LOG10_P`` to keep the value finite when the tail underflows.

    Better calibrated than :func:`z_score` for small expected counts, since it
    uses the actual discrete distribution rather than a normal approximation.
    """
    lam = np.maximum(t.expected, 1e-12)
    with np.errstate(divide="ignore", invalid="ignore"):
        sf = sp_stats.poisson.sf(t.a - 1, lam)
    return _neg_log10(sf)


def hypergeometric_significance(t: Contingency) -> np.ndarray:
    """Hypergeometric (Fisher exact, right tail), as ``-log10 P(X >= a)``.

    The exact probability of seeing at least ``a`` co-occurrences when drawing
    ``n_a`` commits from ``N`` without replacement, of which ``n_b`` contain
    the other file. This is the most statistically rigorous measure in the
    registry -- it makes no asymptotic approximation at all -- and correspondingly
    the most expensive to compute.

    Use it to confirm the pairs that cheaper measures surfaced, rather than as
    the primary ranking pass over millions of pairs.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        sf = sp_stats.hypergeom.sf(t.a - 1, t.n, t.n_a, t.n_b)
    return _neg_log10(sf)


def _neg_log10(p: np.ndarray) -> np.ndarray:
    """Convert a p-value array to ``-log10(p)``, clamped and nan-safe."""
    p = np.asarray(p, dtype=np.float64)
    p = np.where(np.isfinite(p), p, 1.0)
    p = np.clip(p, 10.0**-MAX_NEG_LOG10_P, 1.0)
    return np.clip(-np.log10(p), 0.0, MAX_NEG_LOG10_P)


# --------------------------------------------------------------------------
# Correlation family (signed -- these can express negative coupling)
# --------------------------------------------------------------------------


def phi(t: Contingency) -> np.ndarray:
    """Phi coefficient: Pearson correlation between two binary variables.

    ``(ad - bc) / sqrt(n_a * n_b * (c+d) * (b+d))``. Range [-1, 1].

    The signed counterpart to chi-square (``phi^2 = chi2 / N``), and the most
    directly interpretable correlation here: negative values mean the two files
    actively avoid each other, which is real signal about module boundaries.
    """
    num = t.a * t.d - t.b * t.c
    den = np.sqrt(t.n_a * t.n_b * (t.c + t.d) * (t.b + t.d))
    return safe_div(num, den)


def cramers_v(t: Contingency) -> np.ndarray:
    """Cramer's V: ``sqrt(chi2 / (N * min(rows-1, cols-1)))``.

    For a 2x2 table ``min(r-1, c-1) == 1``, so this reduces exactly to
    ``|phi|``. It is retained under its own name because it is the measure that
    generalises to the larger contingency tables used by the directory-level
    rollups, where the reduction no longer holds.
    """
    return np.sqrt(np.clip(safe_div(chi_square(t), t.n), 0.0, None))


def yules_q(t: Contingency) -> np.ndarray:
    """Yule's Q: ``(ad - bc) / (ad + bc)``. Range [-1, 1].

    Reaches +/-1 whenever any single cell is zero, which makes it very eager --
    a pair that has never once appeared apart scores a perfect 1.0 on two
    observations. Effective at separating sign and direction, poor at
    expressing confidence.
    """
    ad = t.a * t.d
    bc = t.b * t.c
    return safe_div(ad - bc, ad + bc)


def yules_y(t: Contingency) -> np.ndarray:
    """Yule's Y, the coefficient of colligation.

    ``(sqrt(ad) - sqrt(bc)) / (sqrt(ad) + sqrt(bc))``. Range [-1, 1].

    A square-root-damped variant of :func:`yules_q` that is less prone to
    saturating at the extremes, so it discriminates better among strongly
    coupled pairs.
    """
    ad = np.sqrt(np.clip(t.a * t.d, 0.0, None))
    bc = np.sqrt(np.clip(t.b * t.c, 0.0, None))
    return safe_div(ad - bc, ad + bc)


def michael(t: Contingency) -> np.ndarray:
    """Michael's measure: ``4(ad - bc) / ((a + d)^2 + (b + c)^2)``.

    Range [-1, 1]. A non-linear rescaling of the same ``ad - bc`` numerator
    that drives phi and Yule's Q, but normalised by squared sums rather than a
    product of marginals. Because ``d`` appears in the denominator it behaves
    like the matching coefficients on sparse data and compresses toward zero.
    """
    num = 4.0 * (t.a * t.d - t.b * t.c)
    den = np.square(t.a + t.d) + np.square(t.b + t.c)
    return safe_div(num, den)


# --------------------------------------------------------------------------
# Probability / lift family
# --------------------------------------------------------------------------


def association_strength(t: Contingency) -> np.ndarray:
    """Association strength (a.k.a. lift, or probabilistic affinity).

    ``a * N / (n_a * n_b)`` -- observed co-occurrence divided by what
    independence predicts. 1.0 means exactly chance, 2.0 means twice as often
    as chance, 0.0 means never together.

    This is the multiplicative sibling of PMI (``pmi == log2(association
    strength)``) and inherits the same rare-item bias, but its unlogged scale
    is far easier to explain to a human: "these files change together 14x more
    often than chance".
    """
    return safe_div(t.a * t.n, t.n_a * t.n_b)


def confidence_ab(t: Contingency) -> np.ndarray:
    """``P(B | A) = a / n_a`` -- the directional conditional probability.

    Not one of the 29 symmetric measures, but the single most actionable number
    for an agent: "when you touch A, B also changes 80% of the time". Asymmetric
    by construction, so A->B and B->A differ.
    """
    return safe_div(t.a, t.n_a)


def confidence_ba(t: Contingency) -> np.ndarray:
    """``P(A | B) = a / n_b`` -- the reverse conditional probability."""
    return safe_div(t.a, t.n_b)
