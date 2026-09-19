"""Tests for the benchmark statistics.

These are the numbers a decision gets made on, so they are checked against
values worked out by hand rather than against the implementation.
"""

import pytest

from bench.stats import accuracy_gap, mcnemar_exact, wilson_interval


class TestWilson:
    def test_a_half_and_half_result(self):
        interval = wilson_interval(50, 100)
        assert interval.point == 0.5
        # Known value for 50/100 at 95%.
        assert interval.low == pytest.approx(0.4038, abs=1e-3)
        assert interval.high == pytest.approx(0.5962, abs=1e-3)

    def test_a_perfect_score_does_not_claim_certainty(self):
        # The whole reason not to use the normal approximation: it would give
        # [1.0, 1.0] and claim a perfect arm can never fail.
        interval = wilson_interval(100, 100)
        assert interval.point == 1.0
        assert interval.low < 1.0
        assert interval.high == 1.0

    def test_a_zero_score_stays_in_range(self):
        interval = wilson_interval(0, 10)
        assert interval.low == 0.0
        assert 0.0 < interval.high < 1.0

    def test_bounds_never_escape_zero_to_one(self):
        for successes in range(0, 11):
            interval = wilson_interval(successes, 10)
            assert 0.0 <= interval.low <= interval.point <= interval.high <= 1.0

    def test_more_data_narrows_the_interval(self):
        narrow = wilson_interval(500, 1000)
        wide = wilson_interval(5, 10)
        assert (narrow.high - narrow.low) < (wide.high - wide.low)

    def test_no_questions_is_not_a_crash(self):
        assert wilson_interval(0, 0).point == 0.0

    def test_it_renders_readably(self):
        assert str(wilson_interval(50, 100)) == "50.0% [40.4%, 59.6%]"


class TestMcNemar:
    def test_only_the_disagreements_count(self):
        # 10 both right, 70 both wrong: neither contributes evidence.
        first = [True] * 10 + [True] * 20 + [False] * 70
        second = [True] * 10 + [False] * 20 + [False] * 70
        result = mcnemar_exact(first, second)
        assert (result.both, result.neither) == (10, 70)
        assert (result.only_first, result.only_second) == (20, 0)
        assert result.discordant == 20
        assert result.total == 100

    def test_a_one_sided_sweep_is_significant(self):
        result = mcnemar_exact([True] * 20 + [False] * 80, [False] * 100)
        # 2 * 0.5**20 — the exact two-sided binomial tail.
        assert result.p_value == pytest.approx(2 * 0.5**20)

    def test_perfect_agreement_is_no_evidence(self):
        outcomes = [True, False, True, False]
        assert mcnemar_exact(outcomes, outcomes).p_value == 1.0
        assert mcnemar_exact(outcomes, outcomes).discordant == 0

    def test_a_split_disagreement_is_not_significant(self):
        first = [True] * 5 + [False] * 5
        second = [False] * 5 + [True] * 5
        assert mcnemar_exact(first, second).p_value == 1.0

    def test_a_known_small_case(self):
        # 3 discordant, all one way: 2 * (1/8) = 0.25.
        result = mcnemar_exact([True, True, True, False], [False, False, False, False])
        assert result.p_value == pytest.approx(0.25)

    def test_the_arms_must_have_answered_the_same_questions(self):
        # Otherwise the pairing — the whole reason for this test — is a lie.
        with pytest.raises(ValueError, match="same questions"):
            mcnemar_exact([True, False], [True])

    def test_it_is_symmetric_in_its_p_value(self):
        first = [True] * 7 + [False] * 13
        second = [False] * 7 + [True] * 13
        assert mcnemar_exact(first, second).p_value == mcnemar_exact(second, first).p_value


class TestGap:
    def test_points_not_proportions(self):
        assert accuracy_gap([True] * 30 + [False] * 70, [True] * 10 + [False] * 90) == 20.0

    def test_a_negative_gap_means_worse(self):
        assert accuracy_gap([True], [True, True][:1]) == 0.0
        assert accuracy_gap([False, False], [True, False]) == -50.0

    def test_no_questions(self):
        assert accuracy_gap([], []) == 0.0
