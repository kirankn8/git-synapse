"""2x2 contingency-table primitives shared by every association measure.

Every measure in :mod:`git_synapse.stats.measures` is a function of the four cells of
the 2x2 table formed by two items (here: two files) over ``N`` observations
(here: commits)::

                    B present     B absent      total
    A present           a             b          n_a
    A absent            c             d          n_b_not
    total              n_b          n_not        N

where

* ``a``  -- commits touching both A and B (the co-occurrence count)
* ``b``  -- commits touching A but not B
* ``c``  -- commits touching B but not A
* ``d``  -- commits touching neither

In practice the ingest pipeline stores only ``(n_a, n_b, n_ab, n_total)`` per
pair because the remaining cells are fully determined:

    b = n_a - n_ab      c = n_b - n_ab      d = N - n_a - n_b + n_ab

Everything here is vectorised: pass numpy arrays and you get arrays back.
Scalars work too, since numpy treats them as 0-d arrays.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Below this magnitude a denominator is treated as zero and the measure yields
# its documented degenerate value rather than an inf/nan.
EPS = 1e-12

ArrayLike = np.ndarray | float | int


@dataclass(frozen=True)
class Contingency:
    """The four cells of the 2x2 table, plus the marginals, as numpy arrays.

    Constructed via :meth:`from_counts` so that callers only ever have to
    supply the four quantities the database actually stores.
    """

    a: np.ndarray  # both present
    b: np.ndarray  # A only
    c: np.ndarray  # B only
    d: np.ndarray  # neither
    n_a: np.ndarray  # a + b
    n_b: np.ndarray  # a + c
    n: np.ndarray  # a + b + c + d

    @classmethod
    def from_counts(
        cls,
        n_ab: ArrayLike,
        n_a: ArrayLike,
        n_b: ArrayLike,
        n_total: ArrayLike,
    ) -> "Contingency":
        """Build a table from the counts the pair aggregation actually stores.

        Args:
            n_ab: commits touching both items.
            n_a: commits touching item A (its marginal).
            n_b: commits touching item B (its marginal).
            n_total: total commits in the population.
        """
        a = np.asarray(n_ab, dtype=np.float64)
        n_a_arr = np.asarray(n_a, dtype=np.float64)
        n_b_arr = np.asarray(n_b, dtype=np.float64)
        n = np.asarray(n_total, dtype=np.float64)

        a, n_a_arr, n_b_arr, n = np.broadcast_arrays(a, n_a_arr, n_b_arr, n)

        # Clamp to the feasible region. A 2x2 table is only realisable when
        #     max(0, n_a + n_b - N) <= n_ab <= min(n_a, n_b)
        # and marginals do not exceed the population. Counts outside that range
        # describe a table that cannot exist, and would silently produce
        # nonsense such as a phi coefficient of -7.9 rather than failing.
        # Clamping keeps every downstream measure inside its documented range
        # even if an upstream aggregation is ever wrong.
        n_a_arr = np.clip(n_a_arr, 0.0, n)
        n_b_arr = np.clip(n_b_arr, 0.0, n)
        a = np.clip(a, np.maximum(0.0, n_a_arr + n_b_arr - n), np.minimum(n_a_arr, n_b_arr))

        b = n_a_arr - a
        c = n_b_arr - a
        d = n - n_a_arr - n_b_arr + a

        return cls(
            a=a,
            b=b,
            c=c,
            d=d,
            n_a=n_a_arr,
            n_b=n_b_arr,
            n=n,
        )

    @property
    def expected(self) -> np.ndarray:
        """Expected co-occurrence count under independence: ``n_a * n_b / N``."""
        return safe_div(self.n_a * self.n_b, self.n)

    @property
    def shape(self) -> tuple[int, ...]:
        return self.a.shape

    def is_degenerate(self) -> np.ndarray:
        """True where a measure cannot be meaningfully defined.

        A table is degenerate when either item is present in every commit or in
        none of them, which collapses a marginal and leaves the association
        undefined.
        """
        return (
            (self.n <= 0)
            | (self.n_a <= 0)
            | (self.n_b <= 0)
            | (self.n_a >= self.n)
            | (self.n_b >= self.n)
        )


def safe_div(num: np.ndarray, den: np.ndarray, fill: float = 0.0) -> np.ndarray:
    """Element-wise division that yields ``fill`` instead of inf/nan.

    Used everywhere so that a single pathological pair (a file that appears in
    every commit, say) cannot poison a whole batch with nans.
    """
    num = np.asarray(num, dtype=np.float64)
    den = np.asarray(den, dtype=np.float64)
    out = np.full(np.broadcast(num, den).shape, fill, dtype=np.float64)
    ok = np.abs(den) > EPS
    np.divide(num, den, out=out, where=ok)
    return out


def xlogy(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """``x * log(y)`` with the convention ``0 * log(0) == 0``.

    This is the standard entropy convention; without it every contingency table
    containing an empty cell would evaluate to nan.
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    out = np.zeros(np.broadcast(x, y).shape, dtype=np.float64)
    ok = (x > 0) & (y > 0)
    out[ok] = x[ok] * np.log(y[ok])
    return out


def xlog2y(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """``x * log2(y)`` with the convention ``0 * log2(0) == 0``."""
    return xlogy(x, y) / np.log(2.0)
