"""Circuit breaker guarding calls to the Warp HTTP API."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from fusion.domain.errors import CircuitOpenError

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"  # normal operation
    OPEN = "open"  # failing, block requests
    HALF_OPEN = "half_open"  # testing whether the service recovered


class CircuitBreaker:
    """Opens after ``failure_threshold`` consecutive failures; retries after ``timeout``.

    ``is_failure`` decides which exceptions count: by default every one does.
    An exception it rejects (e.g. an HTTP 4xx, which proves the service is
    up) is re-raised without touching the failure count.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        timeout: float = 60.0,
        is_failure: Callable[[BaseException], bool] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        self.name = name
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.is_failure = is_failure
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.last_success_time: float | None = None
        self._lock = threading.Lock()

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker '%s' entering HALF_OPEN state", self.name)
                else:
                    raise CircuitOpenError(
                        f"Circuit breaker '{self.name}' is OPEN. "
                        f"Service unavailable. Retry after {self.timeout}s"
                    )
        try:
            result = func(*args, **kwargs)
        except Exception as e:
            if self.is_failure is None or self.is_failure(e):
                self._on_failure()
            raise
        self._on_success()
        return result

    def _should_attempt_reset(self) -> bool:
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.timeout

    def _on_success(self) -> None:
        with self._lock:
            self.failure_count = 0
            self.last_success_time = time.time()
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                logger.info("Circuit breaker '%s' reset to CLOSED", self.name)

    def _on_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold and self.state != CircuitState.OPEN:
                self.state = CircuitState.OPEN
                logger.error(
                    "Circuit breaker '%s' tripped to OPEN after %d failures",
                    self.name,
                    self.failure_count,
                )

    def reset(self) -> None:
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
        logger.info("Circuit breaker '%s' manually reset", self.name)

    def get_state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "last_failure_time": self.last_failure_time,
            "last_success_time": self.last_success_time,
        }
