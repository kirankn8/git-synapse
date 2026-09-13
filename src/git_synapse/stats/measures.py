"""The 31 association measures, each a pure function of a 2x2 contingency table."""

from __future__ import annotations

import numpy as np
from scipy import special as sp_special
from scipy import stats as sp_stats

from git_synapse.stats.contingency import Contingency, safe_div, xlog2y, xlogy

MAX_NEG_LOG10_P = 1e6




def jaccard(t: Contingency) -> np.ndarray:
    """Jaccard index: intersection over union, ``a / (a + b + c)``."""
    return safe_div(t.a, t.a + t.b + t.c)


def dice(t: Contingency) -> np.ndarray:
    """Dice coefficient: ``2a / (2a + b + c)``."""
    return safe_div(2.0 * t.a, 2.0 * t.a + t.b + t.c)


def sorensen(t: Contingency) -> np.ndarray:
    """Sorensen index -- mathematically identical to :func:`dice`."""
    return dice(t)


def ochiai(t: Contingency) -> np.ndarray:
    """Ochiai coefficient: ``a / sqrt(n_a * n_b)``."""
    return safe_div(t.a, np.sqrt(t.n_a * t.n_b))


def simpson(t: Contingency) -> np.ndarray:
    """Simpson / overlap coefficient: ``a / min(n_a, n_b)``."""
    return safe_div(t.a, np.minimum(t.n_a, t.n_b))


def braun_blanquet(t: Contingency) -> np.ndarray:
    """Braun-Blanquet: ``a / max(n_a, n_b)``."""
    return safe_div(t.a, np.maximum(t.n_a, t.n_b))


def kulczynski(t: Contingency) -> np.ndarray:
    """Kulczynski measure: arithmetic mean of ``P(A|B)`` and ``P(B|A)``."""
    return 0.5 * (safe_div(t.a, t.n_a) + safe_div(t.a, t.n_b))


def fager(t: Contingency) -> np.ndarray:
    """Fager's index: Ochiai minus a small-sample penalty."""
    possible = np.minimum(t.n_a, t.n_b) > 0
    score = ochiai(t) - safe_div(1.0, 2.0 * np.sqrt(np.maximum(t.n_a, t.n_b)))
    return np.where(possible, score, 0.0)




def russell_rao(t: Contingency) -> np.ndarray:
    """Russell-Rao: ``a / N``."""
    return safe_div(t.a, t.n)


def sokal_michener(t: Contingency) -> np.ndarray:
    """Sokal-Michener simple matching: ``(a + d) / N``."""
    return safe_div(t.a + t.d, t.n)


def rogers_tanimoto(t: Contingency) -> np.ndarray:
    """Rogers-Tanimoto: ``(a + d) / (a + d + 2(b + c))``."""
    return safe_div(t.a + t.d, t.a + t.d + 2.0 * (t.b + t.c))


def hamann(t: Contingency) -> np.ndarray:
    """Hamann similarity: ``((a + d) - (b + c)) / N``."""
    return safe_div((t.a + t.d) - (t.b + t.c), t.n)


def faith(t: Contingency) -> np.ndarray:
    """Faith similarity: ``(a + 0.5 * d) / N``."""
    return safe_div(t.a + 0.5 * t.d, t.n)




def pmi(t: Contingency) -> np.ndarray:
    """Pointwise mutual information, in bits: ``log2(a*N / (n_a * n_b))``."""
    ratio = safe_div(t.a * t.n, t.n_a * t.n_b)
    out = np.zeros_like(ratio)
    ok = ratio > 0
    out[ok] = np.log2(ratio[ok])
    return out


def npmi(t: Contingency) -> np.ndarray:
    """Normalised PMI: ``pmi / -log2(a / N)``."""
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
    """Positive PMI: ``max(pmi, 0)``."""
    return np.maximum(pmi(t), 0.0)


def mutual_information(t: Contingency) -> np.ndarray:
    """Mutual information of the full 2x2 table, in bits."""
    n = t.n
    cells = (t.a, t.b, t.c, t.d)
    row = (t.n_a, t.n_a, t.n - t.n_a, t.n - t.n_a)
    col = (t.n_b, t.n - t.n_b, t.n_b, t.n - t.n_b)

    total = np.zeros_like(t.a)
    for obs, r, c in zip(cells, row, col, strict=True):
        p = safe_div(obs, n)
        expected_p = safe_div(r * c, n * n)
        total = total + xlog2y(p, safe_div(p, expected_p, fill=1.0))
    return np.maximum(total, 0.0)




def chi_square(t: Contingency) -> np.ndarray:
    """Pearson's chi-square for a 2x2 table."""
    num = t.n * np.square(t.a * t.d - t.b * t.c)
    den = t.n_a * t.n_b * (t.c + t.d) * (t.b + t.d)
    return safe_div(num, den)


def log_likelihood_ratio(t: Contingency) -> np.ndarray:
    """Log-likelihood ratio G², ``2 * sum O * ln(O / E)`` over all four cells."""
    n = t.n
    cells = (t.a, t.b, t.c, t.d)
    row = (t.n_a, t.n_a, t.n - t.n_a, t.n - t.n_a)
    col = (t.n_b, t.n - t.n_b, t.n_b, t.n - t.n_b)

    total = np.zeros_like(t.a)
    for obs, r, c in zip(cells, row, col, strict=True):
        expected = safe_div(r * c, n)
        total = total + xlogy(obs, safe_div(obs, expected, fill=1.0))
    return np.maximum(2.0 * total, 0.0)


def t_score(t: Contingency) -> np.ndarray:
    """T-score: ``(a - E) / sqrt(a)`` where ``E = n_a * n_b / N``."""
    return safe_div(t.a - t.expected, np.sqrt(np.maximum(t.a, 1.0)))


def z_score(t: Contingency) -> np.ndarray:
    """Z-score: ``(a - E) / sqrt(E)``."""
    return safe_div(t.a - t.expected, np.sqrt(t.expected))


def poisson_significance(t: Contingency) -> np.ndarray:
    """Poisson tail significance, reported as ``-log10 P(X >= a)``."""
    lam = np.maximum(t.expected, 1e-12)
    with np.errstate(divide="ignore", invalid="ignore"):
        log_sf = sp_stats.poisson.logsf(t.a - 1, lam)
        a = np.asarray(t.a, dtype=np.float64)
        log_pmf = -lam + a * np.log(lam) - sp_special.gammaln(a + 1.0)
        log_sf = np.where(np.isfinite(log_sf), log_sf, log_pmf)
    return _neg_log10_from_log(log_sf)


def hypergeometric_significance(t: Contingency) -> np.ndarray:
    """Hypergeometric (Fisher exact, right tail), as ``-log10 P(X >= a)``."""
    with np.errstate(divide="ignore", invalid="ignore"):
        log_sf = sp_stats.hypergeom.logsf(t.a - 1, t.n, t.n_a, t.n_b)
    return _neg_log10_from_log(log_sf)


def _neg_log10_from_log(log_p: np.ndarray) -> np.ndarray:
    """``-log10(p)`` from ``ln(p)``, which is how the tails are computed."""
    log_p = np.asarray(log_p, dtype=np.float64)
    out = np.where(np.isnan(log_p), 0.0, -log_p / np.log(10.0))
    return np.clip(out, 0.0, MAX_NEG_LOG10_P) + 0.0




def phi(t: Contingency) -> np.ndarray:
    """Phi coefficient: Pearson correlation between two binary variables."""
    num = t.a * t.d - t.b * t.c
    den = np.sqrt(t.n_a * t.n_b * (t.c + t.d) * (t.b + t.d))
    return safe_div(num, den)


def cramers_v(t: Contingency) -> np.ndarray:
    """Cramer's V: ``sqrt(chi2 / (N * min(rows-1, cols-1)))``."""
    return np.sqrt(np.clip(safe_div(chi_square(t), t.n), 0.0, None))


def yules_q(t: Contingency) -> np.ndarray:
    """Yule's Q: ``(ad - bc) / (ad + bc)``. Range [-1, 1]."""
    ad = t.a * t.d
    bc = t.b * t.c
    return safe_div(ad - bc, ad + bc)


def yules_y(t: Contingency) -> np.ndarray:
    """Yule's Y, the coefficient of colligation."""
    ad = np.sqrt(np.clip(t.a * t.d, 0.0, None))
    bc = np.sqrt(np.clip(t.b * t.c, 0.0, None))
    return safe_div(ad - bc, ad + bc)


def michael(t: Contingency) -> np.ndarray:
    """Michael's measure: ``4(ad - bc) / ((a + d)^2 + (b + c)^2)``."""
    num = 4.0 * (t.a * t.d - t.b * t.c)
    den = np.square(t.a + t.d) + np.square(t.b + t.c)
    return safe_div(num, den)




def association_strength(t: Contingency) -> np.ndarray:
    """Association strength (a.k.a. lift, or probabilistic affinity)."""
    return safe_div(t.a * t.n, t.n_a * t.n_b)


def confidence_ab(t: Contingency) -> np.ndarray:
    """``P(B | A) = a / n_a`` -- the directional conditional probability."""
    return safe_div(t.a, t.n_a)


def confidence_ba(t: Contingency) -> np.ndarray:
    """``P(A | B) = a / n_b`` -- the reverse conditional probability."""
    return safe_div(t.a, t.n_b)
