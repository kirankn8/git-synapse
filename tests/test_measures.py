"""Validation of the 29 association measures against hand-computed values.

The reference table used throughout is::

    n_ab = 10, n_a = 20, n_b = 30, N = 100
    =>  a = 10, b = 10, c = 20, d = 60

Every expected value below was worked out by hand from the formula in the
measure's docstring, so these tests catch an implementation that is
self-consistent but wrong -- which a round-trip or property test alone would not.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats as sp_stats

from git_synapse.stats import measures as m
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import ALL_KEYS, BY_KEY, CORE_KEYS, MEASURES, resolve

TOL = 1e-9



def random_feasible_tables(seed: int, size: int, n_total: int = 1000) -> Contingency:
    """Generate random but *realisable* 2x2 tables.

    A table only exists when ``max(0, n_a + n_b - N) <= n_ab <= min(n_a, n_b)``.
    Sampling n_ab uniformly over ``[0, min(n_a, n_b)]`` -- the obvious thing to
    do -- silently produces tables with a negative ``d`` cell, which are not
    contingency tables at all and on which no bound can be expected to hold.
    """
    rng = np.random.default_rng(seed)
    n_a = rng.integers(1, n_total, size)
    n_b = rng.integers(1, n_total, size)
    lo = np.maximum(0, n_a + n_b - n_total)
    hi = np.minimum(n_a, n_b)
    # Uniform over the feasible interval, inclusive of both endpoints so the
    # degenerate extremes are exercised too.
    n_ab = lo + (rng.random(size) * (hi - lo + 1)).astype(np.int64)
    n_ab = np.minimum(n_ab, hi)
    return Contingency.from_counts(n_ab, n_a, n_b, n_total)


def test_generator_produces_only_feasible_tables() -> None:
    """Guard the guard: the fixture itself must never emit a negative cell."""
    t = random_feasible_tables(seed=7, size=5000)
    for name, cell in (("a", t.a), ("b", t.b), ("c", t.c), ("d", t.d)):
        assert cell.min() >= 0, f"cell {name} went negative in the generator"


def test_infeasible_counts_are_clamped() -> None:
    """An impossible input must be clamped, not propagated as nonsense.

    n_a = n_b = 900 with N = 1000 forces at least 800 co-occurrences. Asking for
    100 describes a table with d = -700. Before clamping this produced a phi
    coefficient of -7.89, far outside phi's [-1, 1] range.
    """
    t = Contingency.from_counts(n_ab=100, n_a=900, n_b=900, n_total=1000)
    assert float(t.d) >= 0.0
    assert float(t.a) == 800.0, "n_ab should be lifted to the feasible minimum"
    assert -1.0 <= float(m.phi(t)) <= 1.0


@pytest.fixture
def table() -> Contingency:
    """The reference 2x2 table: a=10, b=10, c=20, d=60, N=100."""
    return Contingency.from_counts(n_ab=10, n_a=20, n_b=30, n_total=100)


def val(fn, t) -> float:
    """Evaluate a measure and return it as a plain float."""
    return float(np.asarray(fn(t)).reshape(-1)[0])


# ---------------------------------------------------------------------------
# Contingency construction
# ---------------------------------------------------------------------------


def test_contingency_cells(table: Contingency) -> None:
    assert val(lambda t: t.a, table) == 10.0
    assert val(lambda t: t.b, table) == 10.0
    assert val(lambda t: t.c, table) == 20.0
    assert val(lambda t: t.d, table) == 60.0
    # Cells must sum to the population.
    assert val(lambda t: t.a + t.b + t.c + t.d, table) == 100.0


def test_expected_count(table: Contingency) -> None:
    # E = n_a * n_b / N = 20 * 30 / 100
    assert val(lambda t: t.expected, table) == pytest.approx(6.0, abs=TOL)


# ---------------------------------------------------------------------------
# Hand-computed expected values, measure by measure
# ---------------------------------------------------------------------------

EXPECTED: dict[str, float] = {
    # Similarity & overlap
    "jaccard": 10 / 40,                                     # 0.25
    "dice": 20 / 50,                                        # 0.4
    "sorensen": 20 / 50,                                    # identical to dice
    "ochiai": 10 / math.sqrt(600),                          # 0.4082482905
    "simpson": 10 / 20,                                     # 0.5
    "braun_blanquet": 10 / 30,                              # 0.3333333
    "kulczynski": (10 / 20 + 10 / 30) / 2,                  # 0.4166667
    "fager": 10 / math.sqrt(600) - 1 / (2 * math.sqrt(30)),  # 0.3169612
    # Matching coefficients
    "russell_rao": 10 / 100,                                # 0.1
    "sokal_michener": 70 / 100,                             # 0.7
    "rogers_tanimoto": 70 / 130,                            # 0.5384615
    "hamann": (70 - 30) / 100,                              # 0.4
    "faith": (10 + 30) / 100,                               # 0.4
    # Information theoretic
    "pmi": math.log2(10 * 100 / (20 * 30)),                 # 0.7369656
    "npmi": math.log2(10 / 6) / -math.log2(0.1),            # 0.2218488
    "ppmi": math.log2(10 / 6),                              # same as pmi (positive)
    # Summed over all four cells; see test_mutual_information_derivation.
    "mutual_information": (
        0.10 * math.log2(0.10 / 0.06)
        + 0.10 * math.log2(0.10 / 0.14)
        + 0.20 * math.log2(0.20 / 0.24)
        + 0.60 * math.log2(0.60 / 0.56)
    ),                                                      # 0.0322683997
    # Significance
    "chi_square": 16_000_000 / 3_360_000,                   # 4.7619048
    # G-squared = 2 * sum O ln(O/E) over the four cells.
    "log_likelihood_ratio": 2
    * (
        10 * math.log(10 / 6)
        + 10 * math.log(10 / 14)
        + 20 * math.log(20 / 24)
        + 60 * math.log(60 / 56)
    ),                                                      # 4.4733500496
    "t_score": 4 / math.sqrt(10),                           # 1.2649111
    "z_score": 4 / math.sqrt(6),                            # 1.6329932
    # Correlation
    "phi": 400 / math.sqrt(3_360_000),                      # 0.2182179
    "cramers_v": 400 / math.sqrt(3_360_000),                # == |phi| for 2x2
    "yules_q": 400 / 800,                                   # 0.5
    "yules_y": (math.sqrt(600) - math.sqrt(200)) / (math.sqrt(600) + math.sqrt(200)),
    "michael": 1600 / 5800,                                 # 0.2758621
    # Probability / lift
    "association_strength": 10 * 100 / (20 * 30),           # 1.6666667
    # Directional
    "confidence_ab": 10 / 20,                               # 0.5
    "confidence_ba": 10 / 30,                               # 0.3333333
}


@pytest.mark.parametrize("key,expected", sorted(EXPECTED.items()))
def test_measure_matches_hand_computed_value(
    key: str, expected: float, table: Contingency
) -> None:
    got = val(BY_KEY[key].fn, table)
    assert got == pytest.approx(expected, rel=1e-7, abs=1e-9), f"{key}: {got} != {expected}"


def test_mutual_information_derivation(table: Contingency) -> None:
    """Recompute MI from first principles and compare."""
    n = 100.0
    cells = [(10, 20, 30), (10, 20, 70), (20, 80, 30), (60, 80, 70)]
    expected = sum(
        (o / n) * math.log2((o / n) / ((r / n) * (c / n))) for o, r, c in cells if o > 0
    )
    assert val(m.mutual_information, table) == pytest.approx(expected, abs=1e-12)


def test_log_likelihood_derivation(table: Contingency) -> None:
    """Recompute G-squared from first principles and compare."""
    pairs = [(10, 6.0), (10, 14.0), (20, 24.0), (60, 56.0)]
    expected = 2 * sum(o * math.log(o / e) for o, e in pairs)
    assert val(m.log_likelihood_ratio, table) == pytest.approx(expected, abs=1e-12)


def test_significance_measures_match_scipy(table: Contingency) -> None:
    """Poisson and hypergeometric tails must agree with scipy, in -log10 form."""
    poisson_p = sp_stats.poisson.sf(9, 6.0)
    assert val(m.poisson_significance, table) == pytest.approx(-math.log10(poisson_p), rel=1e-9)

    hyper_p = sp_stats.hypergeom.sf(9, 100, 20, 30)
    assert val(m.hypergeometric_significance, table) == pytest.approx(
        -math.log10(hyper_p), rel=1e-9
    )


# ---------------------------------------------------------------------------
# Structural properties that must hold for every measure
# ---------------------------------------------------------------------------


def test_registry_covers_the_specified_29_measures() -> None:
    """The spec lists 29 symmetric measures; the two directional ones are extra."""
    assert len(CORE_KEYS) == 29
    assert len(ALL_KEYS) == 31
    assert len(set(ALL_KEYS)) == 31, "measure keys must be unique"


def test_dice_and_sorensen_are_identical() -> None:
    """They are the same formula under two names; any divergence is a bug."""
    rng = np.random.default_rng(0)
    n_total = 1000
    n_a = rng.integers(1, 500, 200)
    n_b = rng.integers(1, 500, 200)
    n_ab = np.minimum(rng.integers(0, 200, 200), np.minimum(n_a, n_b))
    t = Contingency.from_counts(n_ab, n_a, n_b, n_total)
    np.testing.assert_allclose(m.dice(t), m.sorensen(t), rtol=0, atol=0)


def test_cramers_v_equals_abs_phi_for_2x2() -> None:
    """For a 2x2 table Cramer's V reduces exactly to |phi|."""
    rng = np.random.default_rng(1)
    n_total = 500
    n_a = rng.integers(1, 200, 300)
    n_b = rng.integers(1, 200, 300)
    n_ab = np.minimum(rng.integers(0, 100, 300), np.minimum(n_a, n_b))
    t = Contingency.from_counts(n_ab, n_a, n_b, n_total)
    np.testing.assert_allclose(m.cramers_v(t), np.abs(m.phi(t)), rtol=1e-9, atol=1e-12)


def test_chi_square_equals_n_times_phi_squared() -> None:
    """The identity chi2 = N * phi^2 must hold."""
    t = Contingency.from_counts(n_ab=10, n_a=20, n_b=30, n_total=100)
    assert val(m.chi_square, t) == pytest.approx(100 * val(m.phi, t) ** 2, rel=1e-9)


def test_pmi_is_log2_of_association_strength() -> None:
    t = Contingency.from_counts(n_ab=10, n_a=20, n_b=30, n_total=100)
    assert val(m.pmi, t) == pytest.approx(math.log2(val(m.association_strength, t)), rel=1e-12)


def test_independence_yields_neutral_values() -> None:
    """When a == E exactly, every association measure sits at its neutral point."""
    # n_a=20, n_b=50, N=100 => E = 10. Set a = 10 for exact independence.
    t = Contingency.from_counts(n_ab=10, n_a=20, n_b=50, n_total=100)
    assert val(m.pmi, t) == pytest.approx(0.0, abs=1e-12)
    assert val(m.npmi, t) == pytest.approx(0.0, abs=1e-12)
    assert val(m.phi, t) == pytest.approx(0.0, abs=1e-12)
    assert val(m.chi_square, t) == pytest.approx(0.0, abs=1e-9)
    assert val(m.log_likelihood_ratio, t) == pytest.approx(0.0, abs=1e-9)
    assert val(m.z_score, t) == pytest.approx(0.0, abs=1e-12)
    assert val(m.yules_q, t) == pytest.approx(0.0, abs=1e-12)
    assert val(m.association_strength, t) == pytest.approx(1.0, abs=1e-12)
    assert val(m.mutual_information, t) == pytest.approx(0.0, abs=1e-12)


def test_perfect_positive_coupling() -> None:
    """Two files that always change together and never apart."""
    t = Contingency.from_counts(n_ab=25, n_a=25, n_b=25, n_total=100)
    assert val(m.jaccard, t) == pytest.approx(1.0)
    assert val(m.dice, t) == pytest.approx(1.0)
    assert val(m.ochiai, t) == pytest.approx(1.0)
    assert val(m.simpson, t) == pytest.approx(1.0)
    assert val(m.braun_blanquet, t) == pytest.approx(1.0)
    assert val(m.npmi, t) == pytest.approx(1.0)
    assert val(m.phi, t) == pytest.approx(1.0)
    assert val(m.yules_q, t) == pytest.approx(1.0)
    assert val(m.confidence_ab, t) == pytest.approx(1.0)


def test_mutual_exclusion_is_negative() -> None:
    """Files that never co-occur must score negatively on the signed measures."""
    t = Contingency.from_counts(n_ab=0, n_a=30, n_b=30, n_total=100)
    assert val(m.jaccard, t) == 0.0
    assert val(m.npmi, t) == pytest.approx(-1.0)
    assert val(m.phi, t) < 0
    assert val(m.yules_q, t) == pytest.approx(-1.0)
    assert val(m.association_strength, t) == 0.0
    assert val(m.ppmi, t) == 0.0  # negative PMI clipped away


@pytest.mark.parametrize("spec", MEASURES, ids=lambda s: s.key)
def test_measure_respects_declared_bounds(spec) -> None:
    """Every measure must stay inside the range its registry entry advertises."""
    t = random_feasible_tables(seed=42, size=4000)

    out = np.asarray(spec.compute(t), dtype=np.float64)
    assert np.all(np.isfinite(out)), f"{spec.key} produced non-finite values"
    if spec.lower is not None:
        assert out.min() >= spec.lower - 1e-9, f"{spec.key} below lower bound {spec.lower}"
    if spec.upper is not None:
        assert out.max() <= spec.upper + 1e-9, f"{spec.key} above upper bound {spec.upper}"


@pytest.mark.parametrize("spec", MEASURES, ids=lambda s: s.key)
def test_measure_is_finite_on_degenerate_tables(spec) -> None:
    """Pathological tables must not produce nan or inf.

    A single file present in every commit, or a pair never seen at all, must not
    be able to poison a whole scoring batch.
    """
    degenerate = Contingency.from_counts(
        n_ab=np.array([0, 0, 5, 100, 0, 1]),
        n_a=np.array([0, 100, 5, 100, 1, 1]),
        n_b=np.array([0, 100, 5, 100, 1, 1]),
        n_total=np.array([100, 100, 5, 100, 1, 1]),
    )
    out = np.asarray(spec.compute(degenerate), dtype=np.float64)
    assert np.all(np.isfinite(out)), f"{spec.key} produced nan/inf: {out}"


@pytest.mark.parametrize("spec", MEASURES, ids=lambda s: s.key)
def test_measure_is_symmetric_where_claimed(spec) -> None:
    """Swapping A and B must not change a symmetric measure."""
    if spec.family == "Directional (asymmetric)":
        pytest.skip("directional measures are asymmetric by design")
    forward = Contingency.from_counts(n_ab=10, n_a=20, n_b=30, n_total=100)
    reverse = Contingency.from_counts(n_ab=10, n_a=30, n_b=20, n_total=100)
    assert val(spec.fn, forward) == pytest.approx(val(spec.fn, reverse), rel=1e-9, abs=1e-12)


def test_confidence_measures_are_asymmetric() -> None:
    t = Contingency.from_counts(n_ab=10, n_a=20, n_b=30, n_total=100)
    assert val(m.confidence_ab, t) != pytest.approx(val(m.confidence_ba, t))


def test_resolve_accepts_aliases_and_rejects_unknown() -> None:
    assert resolve("lift").key == "association_strength"
    assert resolve("cosine").key == "ochiai"
    assert resolve("  NPMI  ").key == "npmi"
    with pytest.raises(KeyError, match="unknown measure"):
        resolve("not_a_measure")


def test_vectorisation_matches_scalar_evaluation() -> None:
    """Batched evaluation must equal element-wise evaluation."""
    n_ab = np.array([1, 5, 10, 25])
    n_a = np.array([10, 20, 20, 25])
    n_b = np.array([10, 40, 30, 25])
    batch = Contingency.from_counts(n_ab, n_a, n_b, 100)
    for spec in MEASURES:
        batched = np.asarray(spec.compute(batch), dtype=np.float64)
        for i in range(len(n_ab)):
            single = Contingency.from_counts(n_ab[i], n_a[i], n_b[i], 100)
            assert batched[i] == pytest.approx(
                val(spec.fn, single), rel=1e-9, abs=1e-12
            ), f"{spec.key} mismatch at index {i}"
