"""The statistics that decide whether an arm is actually better.

Two tests, both chosen because the design is *paired*: every arm answers the
same questions, so the informative quantity is how often they disagree, not how
each scored on its own.

- **McNemar's exact test** on the discordant pairs. Comparing two proportions
  with an unpaired test here would throw away the pairing and overstate the
  variance; the exact binomial form is used rather than the chi-square
  approximation because discordant counts in a 50-100 question set are small.
- **Wilson score intervals** for each arm's accuracy. The normal approximation
  misbehaves near 0 and 1 — exactly where a good arm lands — and can produce
  bounds outside [0, 1].

No SciPy: both are short enough to write exactly, and a benchmark that needs a
scientific stack to report its own numbers is harder to run than it should be.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Interval:
    """A confidence interval for a proportion."""

    point: float
    low: float
    high: float

    def as_dict(self) -> dict[str, float]:
        return {"point": self.point, "low": self.low, "high": self.high}

    def __str__(self) -> str:
        return f"{self.point:.1%} [{self.low:.1%}, {self.high:.1%}]"


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> Interval:
    """Wilson score interval for a binomial proportion (default 95%).

    Args:
        successes: Number of correct answers.
        total: Number of questions attempted.
        z: Standard normal quantile; the default is two-sided 95%.

    Returns:
        The point estimate and its interval, both clamped to [0, 1].
    """
    if total <= 0:
        return Interval(0.0, 0.0, 0.0)
    phat = successes / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    spread = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    low = (centre - spread) / denominator
    high = (centre + spread) / denominator
    # A Wilson interval always contains the point estimate; at 0 and 1 the
    # arithmetic can land a few ULPs the wrong side of it, so the invariant is
    # restored rather than left to rounding.
    return Interval(
        phat,
        min(max(0.0, low), phat),
        max(min(1.0, high), phat),
    )


@dataclass(frozen=True, slots=True)
class McNemarResult:
    """The outcome of a paired comparison between two arms."""

    #: Questions the first arm got right and the second wrong.
    only_first: int
    #: Questions the second arm got right and the first wrong.
    only_second: int
    both: int
    neither: int
    p_value: float

    @property
    def discordant(self) -> int:
        """Pairs where the arms disagreed — the only ones the test uses."""
        return self.only_first + self.only_second

    @property
    def total(self) -> int:
        return self.both + self.neither + self.discordant

    def as_dict(self) -> dict[str, float | int]:
        return {
            "only_first": self.only_first,
            "only_second": self.only_second,
            "both": self.both,
            "neither": self.neither,
            "discordant": self.discordant,
            "p_value": self.p_value,
        }


def mcnemar_exact(first: Sequence[bool], second: Sequence[bool]) -> McNemarResult:
    """Exact (binomial) McNemar test for two paired sequences of outcomes.

    Under the null hypothesis the two arms are equally likely to be the one
    that succeeds when they disagree, so the discordant pairs are Binomial(n,
    0.5) and the two-sided p-value is twice the smaller tail (capped at 1).

    Args:
        first: Per-question correctness for one arm.
        second: Per-question correctness for the other, in the same order.

    Returns:
        The 2x2 counts and the p-value.

    Raises:
        ValueError: If the sequences are not the same length — they must be
            answers to the same questions for the pairing to mean anything.
    """
    if len(first) != len(second):
        raise ValueError(
            f"Paired comparison needs the same questions in both arms: "
            f"got {len(first)} and {len(second)}"
        )
    both = sum(1 for a, b in zip(first, second, strict=True) if a and b)
    neither = sum(1 for a, b in zip(first, second, strict=True) if not a and not b)
    only_first = sum(1 for a, b in zip(first, second, strict=True) if a and not b)
    only_second = sum(1 for a, b in zip(first, second, strict=True) if b and not a)

    return McNemarResult(
        only_first=only_first,
        only_second=only_second,
        both=both,
        neither=neither,
        p_value=_two_sided_binomial(min(only_first, only_second), only_first + only_second),
    )


def _two_sided_binomial(smaller_tail: int, discordant: int) -> float:
    """Two-sided p-value for Binomial(discordant, 0.5).

    With no disagreement at all there is no evidence either way, which is a
    p-value of 1 rather than an error.
    """
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(smaller_tail + 1))
    return min(1.0, 2 * tail / (2**discordant))


def accuracy_gap(first: Sequence[bool], second: Sequence[bool]) -> float:
    """Percentage points by which ``first`` beats ``second``.

    The plan's decision thresholds are written in points, so the report needs
    this alongside the p-value: a difference can be significant and still too
    small to act on, and the other way round.
    """
    if not first:
        return 0.0
    return 100.0 * (sum(first) - sum(second)) / len(first)
