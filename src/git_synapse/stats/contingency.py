"""2x2 contingency-table primitives shared by every association measure."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EPS = 1e-12

ArrayLike = np.ndarray | float | int


@dataclass(frozen=True)
class Contingency:
    """The four cells of the 2x2 table, plus the marginals, as numpy arrays."""

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
    ) -> Contingency:
        """Build a table from the counts the pair aggregation actually stores."""
        a = np.asarray(n_ab, dtype=np.float64)
        n_a_arr = np.asarray(n_a, dtype=np.float64)
        n_b_arr = np.asarray(n_b, dtype=np.float64)
        n = np.asarray(n_total, dtype=np.float64)

        a, n_a_arr, n_b_arr, n = np.broadcast_arrays(a, n_a_arr, n_b_arr, n)

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
        """True where a measure cannot be meaningfully defined."""
        return (
            (self.n <= 0)
            | (self.n_a <= 0)
            | (self.n_b <= 0)
            | (self.n_a >= self.n)
            | (self.n_b >= self.n)
        )


def safe_div(num: np.ndarray, den: np.ndarray, fill: float = 0.0) -> np.ndarray:
    """Element-wise division that yields ``fill`` instead of inf/nan."""
    num = np.asarray(num, dtype=np.float64)
    den = np.asarray(den, dtype=np.float64)
    out = np.full(np.broadcast(num, den).shape, fill, dtype=np.float64)
    ok = np.abs(den) > EPS
    np.divide(num, den, out=out, where=ok)
    return out


def xlogy(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """``x * log(y)`` with the convention ``0 * log(0) == 0``."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    out = np.zeros(np.broadcast(x, y).shape, dtype=np.float64)
    ok = (x > 0) & (y > 0)
    out[ok] = x[ok] * np.log(y[ok])
    return out


def xlog2y(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """``x * log2(y)`` with the convention ``0 * log2(0) == 0``."""
    return xlogy(x, y) / np.log(2.0)
