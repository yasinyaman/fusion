"""Tests for the CircuitBreaker reliability primitive."""

import pytest

from fusion.utils.circuit_breaker import CircuitBreaker, CircuitState


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

    def test_open_blocks_further_calls(self):
        cb = CircuitBreaker("t", failure_threshold=1, timeout=60)
        with pytest.raises(ValueError):
            cb.call(_boom)
        assert cb.state == CircuitState.OPEN
        # While OPEN, calls are short-circuited without invoking the function
        with pytest.raises(Exception, match="is OPEN"):
            cb.call(lambda: "should not run")

    def test_half_open_recovers_to_closed(self):
        cb = CircuitBreaker("t", failure_threshold=1, timeout=1)
        with pytest.raises(ValueError):
            cb.call(_boom)
        assert cb.state == CircuitState.OPEN
        # Simulate the timeout window elapsing -> next call goes HALF_OPEN,
        # and a success closes the circuit again.
        cb.last_failure_time -= 10
        assert cb.call(lambda: "ok") == "ok"
        assert cb.state == CircuitState.CLOSED

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("t", failure_threshold=3, timeout=60)
        with pytest.raises(ValueError):
            cb.call(_boom)
        assert cb.failure_count == 1
        cb.call(lambda: 1)
        assert cb.failure_count == 0

    def test_manual_reset(self):
        cb = CircuitBreaker("t", failure_threshold=1, timeout=60)
        with pytest.raises(ValueError):
            cb.call(_boom)
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_get_state(self):
        cb = CircuitBreaker("t", failure_threshold=5, timeout=30)
        st = cb.get_state()
        assert st["name"] == "t"
        assert st["state"] == "closed"
        assert st["failure_threshold"] == 5
