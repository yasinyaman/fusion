"""Tests for the circuit breaker."""

import pytest

from fusion.adapters.outbound.warp.circuit_breaker import CircuitBreaker, CircuitState
from fusion.domain.errors import CircuitOpenError, ConnectionError


def _boom():
    raise ValueError("fail")


class TestCircuitBreaker:
    def test_success_stays_closed(self):
        cb = CircuitBreaker("t", failure_threshold=3, timeout=60)
        assert cb.call(lambda: 42) == 42
        assert cb.state == CircuitState.CLOSED

    def test_trips_open_after_threshold(self):
        cb = CircuitBreaker("t", failure_threshold=2, timeout=60)
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(_boom)
        assert cb.state == CircuitState.OPEN

    def test_open_blocks_further_calls_with_domain_error(self):
        cb = CircuitBreaker("t", failure_threshold=1, timeout=60)
        with pytest.raises(ValueError):
            cb.call(_boom)
        with pytest.raises(CircuitOpenError, match="is OPEN") as exc:
            cb.call(lambda: "should not run")
        assert isinstance(exc.value, ConnectionError)

    def test_half_open_recovers_to_closed(self):
        cb = CircuitBreaker("t", failure_threshold=1, timeout=1)
        with pytest.raises(ValueError):
            cb.call(_boom)
        cb.last_failure_time -= 10  # pretend the timeout window elapsed
        assert cb.call(lambda: "ok") == "ok"
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker("t", failure_threshold=1, timeout=1)
        with pytest.raises(ValueError):
            cb.call(_boom)
        cb.last_failure_time -= 10
        with pytest.raises(ValueError):
            cb.call(_boom)
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("t", failure_threshold=3, timeout=60)
        with pytest.raises(ValueError):
            cb.call(_boom)
        assert cb.failure_count == 1
        cb.call(lambda: 1)
        assert cb.failure_count == 0

    def test_manual_reset_and_state(self):
        cb = CircuitBreaker("t", failure_threshold=1, timeout=30)
        with pytest.raises(ValueError):
            cb.call(_boom)
        cb.reset()
        st = cb.get_state()
        assert st["name"] == "t"
        assert st["state"] == "closed"
        assert st["failure_count"] == 0
        assert st["failure_threshold"] == 1

    def test_is_failure_predicate_skips_counting(self):
        cb = CircuitBreaker(
            "t", failure_threshold=1, timeout=60, is_failure=lambda e: not isinstance(e, KeyError)
        )

        def missing():
            raise KeyError("404-ish")

        for _ in range(3):
            with pytest.raises(KeyError):
                cb.call(missing)
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.last_failure_time is None
        with pytest.raises(ValueError):
            cb.call(_boom)  # still counted
        assert cb.state == CircuitState.OPEN

    def test_ignored_exception_does_not_count_as_success(self):
        cb = CircuitBreaker("t", failure_threshold=2, timeout=60, is_failure=lambda e: False)
        cb.failure_count = 1
        with pytest.raises(ValueError):
            cb.call(_boom)
        assert cb.failure_count == 1  # neither reset nor incremented

    def test_validates_arguments(self):
        with pytest.raises(ValueError):
            CircuitBreaker("t", failure_threshold=0)
        with pytest.raises(ValueError):
            CircuitBreaker("t", timeout=0)
